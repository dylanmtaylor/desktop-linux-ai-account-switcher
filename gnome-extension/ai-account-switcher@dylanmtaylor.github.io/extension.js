// AI Account Switcher: GNOME Shell top-bar menu for Claude Code and Codex accounts.
// The account logic lives in the `ai-account-switcher` backend; this file only draws
// its JSON and calls it asynchronously, so the shell never blocks on disk, keyring
// or network.
// SPDX-License-Identifier: MIT OR Apache-2.0

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Dialog from 'resource:///org/gnome/shell/ui/dialog.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async');

const BACKEND = 'ai-account-switcher';
// Lets the app launcher (`ai-account-switcher open`) pop this menu open.
const BUS_PATH = '/io/github/dylanmtaylor/AiAccountSwitcher';
const BUS_IFACE = `<node>
  <interface name="io.github.dylanmtaylor.AiAccountSwitcher">
    <method name="Open"/>
  </interface>
</node>`;
const BAR_WIDTH = 240;
const DOT = PopupMenu.Ornament.DOT;
const NO_DOT = PopupMenu.Ornament.NO_DOT;

function findBackend() {
    const local = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', BACKEND]);
    if (GLib.file_test(local, GLib.FileTest.IS_EXECUTABLE))
        return local;
    return GLib.find_program_in_path(BACKEND);
}

class Backend {
    constructor() {
        this._cancellable = new Gio.Cancellable();
    }

    async run(args) {
        const path = findBackend();
        if (!path)
            throw new Error(`The ${BACKEND} backend is not installed. Run install.sh from the project.`);
        const launcher = new Gio.SubprocessLauncher({
            flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
        });
        const localBin = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin']);
        launcher.setenv('PATH', `${localBin}:${GLib.getenv('PATH') ?? '/usr/bin:/bin'}`, true);
        const proc = launcher.spawnv([path, '--json', ...args]);
        const [stdout, stderr] = await proc.communicate_utf8_async(null, this._cancellable);
        let result;
        try {
            result = JSON.parse(stdout);
        } catch {
            throw new Error(stderr?.trim() || `${BACKEND} returned no data`);
        }
        if (!result.ok)
            throw new Error(result.error ?? 'Unknown error');
        return result;
    }

    destroy() {
        this._cancellable.cancel();
    }
}

const ConfirmDialog = GObject.registerClass(
class ConfirmDialog extends ModalDialog.ModalDialog {
    _init(title, body, actionLabel, onConfirm) {
        super._init({styleClass: 'aias-dialog'});
        this.contentLayout.add_child(new Dialog.MessageDialogContent({title, description: body}));
        this.setButtons([
            {label: 'Cancel', action: () => this.close(), key: Clutter.KEY_Escape},
            {
                label: actionLabel,
                action: () => {
                    this.close();
                    onConfirm();
                },
                default: true,
            },
        ]);
    }
});

const NameDialog = GObject.registerClass(
class NameDialog extends ModalDialog.ModalDialog {
    _init(title, description, onAccept) {
        super._init({styleClass: 'aias-dialog'});
        this.contentLayout.add_child(new Dialog.MessageDialogContent({title, description}));
        this._entry = new St.Entry({
            style_class: 'aias-dialog-entry',
            hint_text: 'e.g. work or you@example.com',
            can_focus: true,
        });
        this.contentLayout.add_child(this._entry);
        const accept = () => {
            const name = this._entry.get_text().trim();
            if (!name)
                return;
            this.close();
            onAccept(name);
        };
        this._entry.clutter_text.connect('activate', accept);
        this.setButtons([
            {label: 'Cancel', action: () => this.close(), key: Clutter.KEY_Escape},
            {label: 'Create', action: accept, default: true},
        ]);
        this.setInitialKeyFocus(this._entry);
    }
});

function usageBar(percent) {
    const trough = new St.Widget({style_class: 'aias-bar-trough', width: BAR_WIDTH});
    const fill = new St.Widget({
        style_class: 'aias-bar-fill',
        width: Math.max(2, Math.round((BAR_WIDTH * Math.min(percent, 100)) / 100)),
    });
    if (percent >= 90)
        fill.add_style_class_name('critical');
    else if (percent >= 70)
        fill.add_style_class_name('warning');
    trough.add_child(fill);
    return trough;
}

function ageText(seconds) {
    if (seconds === null || seconds === undefined)
        return '';
    if (seconds < 60)
        return 'just now';
    if (seconds < 3600)
        return `${Math.floor(seconds / 60)} min ago`;
    return `${Math.floor(seconds / 3600)} h ago`;
}

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'AI Account Switcher');
        this._extension = extension;
        this._backend = new Backend();
        this._status = null;
        this._error = null;
        this._refreshing = false;
        this._timerId = 0;
        this._pollId = 0;
        this._kickId = 0;

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        box.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${extension.path}/icons/ai-account-switcher-symbolic.svg`),
            style_class: 'system-status-icon',
        }));
        this._label = new St.Label({
            text: '',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'aias-panel-label',
            visible: false,
        });
        box.add_child(this._label);
        this.add_child(box);

        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._loadStatus();
        });

        this._render();
        this._refresh(true);
    }

    // ------------------------------------------------------------------ backend calls

    _scheduleRefresh() {
        if (this._timerId)
            GLib.source_remove(this._timerId);
        const minutes = Math.max(1, this._status?.settings?.refresh_minutes ?? 5);
        this._timerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, minutes * 60, () => {
            this._refresh(true);
            return GLib.SOURCE_CONTINUE;
        });
    }

    async _call(args, {render = true} = {}) {
        try {
            const result = await this._backend.run(args);
            if (this._destroyed)
                return null;
            this._status = result;
            this._error = null;
            return result;
        } catch (e) {
            if (this._destroyed || e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                return null;
            this._error = e.message;
            console.warn(`[ai-account-switcher] ${args.join(' ')}: ${e.message}`);
            return null;
        } finally {
            if (render && !this._destroyed)
                this._render();
        }
    }

    async _loadStatus() {
        await this._call(['status']);
        this._pollWhilePending();
    }

    async _refresh(notify = false) {
        if (this._refreshing)
            return;
        this._refreshing = true;
        this._render();
        await this._call(notify ? ['--notify', 'refresh'] : ['refresh']);
        this._refreshing = false;
        if (!this._destroyed) {
            this._render();
            this._scheduleRefresh();
        }
    }

    _action(args) {
        this.menu.close();
        this._call(['--notify', ...args]).then(() => this._pollWhilePending());
    }

    _add(provider) {
        if (provider.add_needs_name) {
            this.menu.close();
            new NameDialog(
                `New ${provider.label} profile`,
                `${provider.label} opens on a fresh profile so you can sign in. ` +
                'Name it after the account, e.g. its email.',
                name => this._action(['add', provider.id, '--name', name])
            ).open();
            return;
        }
        const providerId = provider.id;
        this.menu.close();
        // Logins run in a terminal and may take minutes; show "waiting" meanwhile.
        this._call(['--notify', 'add', providerId]).then(() => this._pollWhilePending());
        if (this._kickId)
            GLib.source_remove(this._kickId);
        this._kickId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 800, () => {
            this._kickId = 0;
            this._loadStatus();
            return GLib.SOURCE_REMOVE;
        });
    }

    _pollWhilePending() {
        const pending = this._status?.providers?.some(p => p.pending);
        if (!pending || this._pollId || this._destroyed)
            return;
        this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 3, () => {
            this._pollId = 0;
            if (!this._destroyed)
                this._loadStatus();
            return GLib.SOURCE_REMOVE;
        });
    }

    _confirmRemove(provider, account) {
        this.menu.close();
        new ConfirmDialog(
            `Remove ${account.email}?`,
            provider.remove_hint ??
            `The saved ${provider.label} login for this account is deleted from this computer. ` +
            'Your account itself is not affected, and you can add it again any time.',
            'Remove',
            () => this._action(['remove', provider.id, account.id])
        ).open();
    }

    // ------------------------------------------------------------------ drawing

    _render() {
        if (this._destroyed)
            return;
        const status = this._status;
        const panelText = status?.panel?.text ?? '';
        this._label.text = panelText;
        this._label.visible = panelText !== '';

        this.menu.removeAll();
        if (this._error)
            this._addNotice(this._error);

        if (!status) {
            this.menu.addMenuItem(new PopupMenu.PopupMenuItem('Loading accounts…', {reactive: false}));
            return;
        }

        for (const provider of status.providers)
            this._renderProvider(provider);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const ago = status.last_refresh ? ageText(Date.now() / 1000 - status.last_refresh) : '';
        const refresh = new PopupMenu.PopupImageMenuItem(
            this._refreshing ? 'Refreshing usage…' : `Refresh usage${ago ? `  (updated ${ago})` : ''}`,
            'view-refresh-symbolic'
        );
        refresh.setSensitive(!this._refreshing);
        refresh.connect('activate', () => this._refresh(false));
        this.menu.addMenuItem(refresh);

        const options = new PopupMenu.PopupSubMenuMenuItem('Options');
        for (const provider of status.providers.filter(p => p.manageable !== false)) {
            const toggle = new PopupMenu.PopupSwitchMenuItem(
                `Auto-switch ${provider.label} at limit`, provider.auto_switch
            );
            toggle.connect('toggled', (_item, state) =>
                this._call(['auto-switch', provider.id, state ? 'on' : 'off']));
            options.menu.addMenuItem(toggle);
        }
        const showUsage = new PopupMenu.PopupSwitchMenuItem(
            'Show usage in the top bar', status.settings.show_usage_in_panel
        );
        showUsage.connect('toggled', (_item, state) =>
            this._call(['set', 'show_usage_in_panel', state ? 'on' : 'off']));
        options.menu.addMenuItem(showUsage);
        this.menu.addMenuItem(options);
    }

    _addNotice(text) {
        const item = new PopupMenu.PopupMenuItem(text, {reactive: false});
        item.label.add_style_class_name('aias-notice');
        item.label.clutter_text.line_wrap = true;
        this.menu.addMenuItem(item);
    }

    _renderProvider(provider) {
        const header = new PopupMenu.PopupSeparatorMenuItem(provider.label);
        header.label.add_style_class_name('aias-provider-header');
        this.menu.addMenuItem(header);

        if (!provider.cli_found && provider.accounts.length === 0) {
            this._addNotice(`${provider.label} is not installed.`);
            return;
        }
        if (provider.notice)
            this._addNotice(provider.notice);
        if (provider.unsaved_login)
            this._addNotice(`Signed in as ${provider.unsaved_login}; it is saved on the next refresh.`);

        for (const account of provider.accounts)
            this._renderAccount(provider, account);

        if (provider.manageable === false) {
            // Read-only provider (Gemini via agy): nothing to add or switch.
        } else if (provider.pending) {
            const waiting = new PopupMenu.PopupImageMenuItem(
                'Waiting for sign-in in the terminal…', 'content-loading-symbolic', {reactive: false}
            );
            this.menu.addMenuItem(waiting);
        } else if (provider.cli_found) {
            const noun = provider.add_needs_name ? 'profile' : 'account';
            const add = new PopupMenu.PopupImageMenuItem(
                provider.accounts.length || provider.add_needs_name
                    ? `Add ${provider.label} ${noun}…` : `Sign in to ${provider.label}…`,
                'list-add-symbolic'
            );
            add.connect('activate', () => this._add(provider));
            this.menu.addMenuItem(add);
        }
    }

    _renderAccount(provider, account) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: true, can_focus: true});
        item.setOrnament(account.active ? DOT : NO_DOT);

        const email = new St.Label({text: account.email, y_align: Clutter.ActorAlign.CENTER, x_expand: true});
        item.add_child(email);
        const details = [account.plan, account.has_credentials ? '' : 'login missing'].filter(Boolean);
        if (details.length) {
            item.add_child(new St.Label({
                text: details.join(' · '),
                style_class: 'aias-account-plan',
                y_align: Clutter.ActorAlign.CENTER,
            }));
        }
        const manageable = provider.manageable !== false;
        const removable = account.removable ?? !account.active;
        const clickable = manageable && (!account.active || account.launchable === true);
        if (manageable && removable) {
            const remove = new St.Button({
                style_class: 'aias-remove-button',
                child: new St.Icon({icon_name: 'user-trash-symbolic', icon_size: 14}),
                accessible_name: `Remove ${account.email}`,
                y_align: Clutter.ActorAlign.CENTER,
            });
            remove.connect('clicked', () => this._confirmRemove(provider, account));
            item.add_child(remove);
        }
        item.connect('activate', () => {
            if (clickable)
                this._action(['switch', provider.id, account.id]);
        });
        this.menu.addMenuItem(item);

        const usage = account.usage;
        const row = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        row.add_style_class_name('aias-usage-row');
        row.setOrnament(PopupMenu.Ornament.NONE); // keep the indent, no radio ring
        const box = new St.BoxLayout({
            style_class: 'aias-usage-box',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });
        if (!usage) {
            box.add_child(new St.Label({text: 'Usage not checked yet', style_class: 'aias-usage-dim'}));
        } else if (!usage.available) {
            box.add_child(new St.Label({text: usage.message, style_class: 'aias-usage-dim'}));
        } else {
            for (const w of usage.windows) {
                const reset = w.resets_in ? `resets in ${w.resets_in}${w.resets_at_local ? ` (${w.resets_at_local})` : ''}` : '';
                box.add_child(new St.Label({
                    text: `${w.label}  ${Math.round(w.percent)}%${reset ? `  ·  ${reset}` : ''}`,
                    style_class: 'aias-usage-text',
                }));
                box.add_child(usageBar(w.percent));
            }
        }
        row.add_child(box);
        this.menu.addMenuItem(row);
    }

    destroy() {
        this._destroyed = true;
        if (this._timerId)
            GLib.source_remove(this._timerId);
        if (this._pollId)
            GLib.source_remove(this._pollId);
        if (this._kickId)
            GLib.source_remove(this._kickId);
        this._timerId = this._pollId = this._kickId = 0;
        this._backend.destroy();
        super.destroy();
    }
});

export default class AiAccountSwitcherExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
        this._dbus = Gio.DBusExportedObject.wrapJSObject(BUS_IFACE, {
            Open: () => this._indicator?.menu.open(),
        });
        this._dbus.export(Gio.DBus.session, BUS_PATH);
    }

    disable() {
        this._dbus?.unexport();
        this._dbus = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
