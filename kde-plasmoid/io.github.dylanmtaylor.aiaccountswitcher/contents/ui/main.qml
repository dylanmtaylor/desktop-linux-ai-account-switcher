// AI Account Switcher: Plasma 6 panel widget for Claude Code and Codex accounts.
// All account logic lives in the `ai-account-switcher` backend; this widget only
// draws its JSON and runs it asynchronously.
// SPDX-License-Identifier: MIT OR Apache-2.0

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasma5support as P5Support
import org.kde.plasma.plasmoid

PlasmoidItem {
    id: root

    property var status: null
    property string errorText: ""
    property bool refreshing: false
    property var pendingRemoval: null   // {provider, account}
    property int seq: 0
    property var callbacks: ({})

    readonly property string panelText: status && status.panel ? status.panel.text : ""
    // The system tray draws its own heading and wants icon-only entries.
    readonly property bool inTray: (Plasmoid.containmentDisplayHints & PlasmaCore.Types.ContainmentDrawsPlasmoidHeading)
    readonly property bool anyPending: status ? status.providers.some(p => p.pending) : false

    toolTipMainText: "AI Account Switcher"
    Plasmoid.status: status && status.providers.some(p => p.active) ? PlasmaCore.Types.ActiveStatus
                                                                     : PlasmaCore.Types.PassiveStatus
    toolTipSubText: errorText !== "" ? errorText : (status ? status.panel.tooltip : "Loading…")

    // ------------------------------------------------------------------ backend

    function shellQuote(s) {
        return "'" + String(s).replace(/'/g, "'\\''") + "'";
    }

    function run(args, done) {
        seq += 1;
        // The executable engine runs a shell command; the trailing comment keeps
        // identical concurrent calls distinct.
        const cmd = 'B="$HOME/.local/bin/ai-account-switcher"; [ -x "$B" ] || B=ai-account-switcher; '
            + 'exec "$B" --json ' + args.map(shellQuote).join(" ") + " #" + seq;
        callbacks[cmd] = done;
        executable.connectSource(cmd);
    }

    function call(args, after) {
        run(args, (code, stdout, stderr) => {
            let result = null;
            try {
                result = JSON.parse(stdout);
            } catch (e) {
                errorText = stderr.trim() !== "" ? stderr.trim()
                    : "ai-account-switcher backend not found. Run install.sh from the project.";
            }
            if (result) {
                if (result.ok) {
                    status = result;
                    errorText = "";
                } else {
                    errorText = result.error || "Unknown error";
                }
            }
            if (after)
                after();
        });
    }

    function loadStatus() {
        call(["status"]);
    }

    function refresh(notify) {
        if (refreshing)
            return;
        refreshing = true;
        call(notify ? ["--notify", "refresh"] : ["refresh"], () => { refreshing = false; });
    }

    function action(args) {
        call(["--notify"].concat(args));
    }

    function addAccount(providerId, name) {
        if (name) {
            call(["--notify", "add", providerId, "--name", name]);
            return;
        }
        call(["--notify", "add", providerId]);
        statusKick.restart();   // show "waiting for sign-in" quickly
    }

    P5Support.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []
        onNewData: (sourceName, data) => {
            const done = root.callbacks[sourceName];
            delete root.callbacks[sourceName];
            disconnectSource(sourceName);
            if (done)
                done(data["exit code"], data["stdout"] || "", data["stderr"] || "");
        }
    }

    Timer {
        id: refreshTimer
        interval: Math.max(1, root.status ? root.status.settings.refresh_minutes : 5) * 60000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refresh(true)
    }

    Timer {
        id: statusKick
        interval: 800
        onTriggered: root.loadStatus()
    }

    Timer {
        interval: 3000
        running: root.anyPending
        repeat: true
        onTriggered: root.loadStatus()
    }

    onExpandedChanged: {
        if (expanded)
            loadStatus();
        else
            pendingRemoval = null;
    }

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: "Refresh Usage"
            icon.name: "view-refresh"
            onTriggered: root.refresh(false)
        }
    ]

    // ------------------------------------------------------------------ panel

    compactRepresentation: MouseArea {
        id: compact
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton
        onClicked: root.expanded = !root.expanded

        readonly property bool horizontal: Plasmoid.formFactor === PlasmaCore.Types.Horizontal
        Layout.minimumWidth: horizontal && !root.inTray ? row.implicitWidth : Kirigami.Units.iconSizes.small
        Layout.preferredWidth: Layout.minimumWidth

        RowLayout {
            id: row
            anchors.centerIn: parent
            height: parent.height
            spacing: Kirigami.Units.smallSpacing

            Kirigami.Icon {
                Layout.preferredHeight: Math.min(compact.height, Kirigami.Units.iconSizes.medium)
                Layout.preferredWidth: Layout.preferredHeight
                source: Qt.resolvedUrl("../icons/ai-account-switcher.svg")
                isMask: true
                color: Kirigami.Theme.textColor
                active: compact.containsMouse
            }
            PlasmaComponents.Label {
                visible: compact.horizontal && !root.inTray && text !== ""
                text: root.panelText
            }
        }
    }

    // ------------------------------------------------------------------ popup

    fullRepresentation: PlasmaExtras.Representation {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 22
        Layout.preferredWidth: Kirigami.Units.gridUnit * 24
        Layout.preferredHeight: Kirigami.Units.gridUnit * 28
        collapseMarginsHint: true

        header: PlasmaExtras.PlasmoidHeading {
            visible: !root.inTray
            RowLayout {
                anchors.fill: parent
                Kirigami.Heading {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.smallSpacing
                    level: 2
                    text: "AI Accounts"
                }
                PlasmaComponents.ToolButton {
                    icon.name: "view-refresh"
                    enabled: !root.refreshing
                    display: PlasmaComponents.AbstractButton.IconOnly
                    text: "Refresh usage"
                    PlasmaComponents.ToolTip.text: text
                    PlasmaComponents.ToolTip.visible: hovered
                    onClicked: root.refresh(false)
                }
            }
        }

        footer: PlasmaExtras.PlasmoidHeading {
            position: PlasmaComponents.ToolBar.Footer
            contentItem: ColumnLayout {
                RowLayout {
                    visible: root.pendingRemoval !== null
                    Layout.fillWidth: true
                    PlasmaComponents.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        text: root.pendingRemoval
                            ? "Remove " + root.pendingRemoval.account.email + "? "
                              + (root.pendingRemoval.provider.remove_hint || "Its saved login is deleted from this computer; the account itself is not affected.")
                            : ""
                    }
                    PlasmaComponents.Button {
                        icon.name: "edit-delete"
                        text: "Remove"
                        onClicked: {
                            const r = root.pendingRemoval;
                            root.pendingRemoval = null;
                            root.action(["remove", r.provider.id, r.account.id]);
                        }
                    }
                    PlasmaComponents.Button {
                        text: "Cancel"
                        onClicked: root.pendingRemoval = null
                    }
                }
                RowLayout {
                    visible: root.pendingRemoval === null
                    Layout.fillWidth: true
                    PlasmaComponents.CheckBox {
                        text: "Show usage in panel"
                        checked: root.status ? root.status.settings.show_usage_in_panel : true
                        onToggled: root.call(["set", "show_usage_in_panel", checked ? "on" : "off"])
                    }
                    PlasmaComponents.CheckBox {
                        text: "Show reset time"
                        visible: root.status ? root.status.settings.show_usage_in_panel : true
                        checked: root.status && root.status.settings.show_reset_in_panel !== undefined
                            ? root.status.settings.show_reset_in_panel : true
                        onToggled: root.call(["set", "show_reset_in_panel", checked ? "on" : "off"])
                    }
                    Item { Layout.fillWidth: true }
                    PlasmaComponents.Label {
                        opacity: 0.6
                        text: root.refreshing ? "Refreshing…" : root.ageText(root.status ? root.status.last_refresh : null)
                    }
                }
            }
        }

        contentItem: PlasmaComponents.ScrollView {
            id: scroll
            contentWidth: availableWidth

            ColumnLayout {
                width: scroll.availableWidth
                spacing: Kirigami.Units.largeSpacing

                Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: root.errorText !== ""
                    type: Kirigami.MessageType.Error
                    text: root.errorText
                }

                PlasmaComponents.BusyIndicator {
                    visible: root.status === null && root.errorText === ""
                    Layout.alignment: Qt.AlignHCenter
                }

                Repeater {
                    model: root.status ? root.status.providers : []
                    delegate: ProviderSection {
                        Layout.fillWidth: true
                        provider: modelData
                    }
                }
            }
        }
    }

    function ageText(ts) {
        if (!ts)
            return "";
        const s = Date.now() / 1000 - ts;
        if (s < 60)
            return "Updated just now";
        if (s < 3600)
            return "Updated " + Math.floor(s / 60) + " min ago";
        return "Updated " + Math.floor(s / 3600) + " h ago";
    }

    component ProviderSection: ColumnLayout {
        id: section
        property var provider
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.smallSpacing
            Layout.rightMargin: Kirigami.Units.smallSpacing
            Kirigami.Heading {
                Layout.fillWidth: true
                level: 4
                text: provider.label
            }
            PlasmaComponents.Switch {
                visible: provider.manageable !== false && provider.accounts.length > 1
                text: "Auto-switch at limit"
                checked: provider.auto_switch
                onToggled: root.call(["auto-switch", provider.id, checked ? "on" : "off"])
            }
        }

        PlasmaComponents.Label {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.smallSpacing
            visible: !provider.cli_found && provider.accounts.length === 0
            opacity: 0.7
            text: provider.label + " is not installed."
        }

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: !!provider.notice || !!provider.unsaved_login
            type: Kirigami.MessageType.Warning
            text: provider.notice ? provider.notice
                : "Signed in as " + provider.unsaved_login + "; it is saved on the next refresh."
        }

        Repeater {
            model: provider.accounts
            delegate: AccountRow {
                Layout.fillWidth: true
                provider: section.provider
                account: modelData
            }
        }

        RowLayout {
            visible: !!provider.pending
            Layout.leftMargin: Kirigami.Units.smallSpacing
            PlasmaComponents.BusyIndicator {
                Layout.preferredHeight: Kirigami.Units.iconSizes.small
                Layout.preferredWidth: Kirigami.Units.iconSizes.small
                running: visible
            }
            PlasmaComponents.Label { text: "Waiting for sign-in in the terminal…" }
        }

        PlasmaComponents.Button {
            visible: provider.manageable !== false && provider.cli_found && !provider.pending && !nameRow.visible
            icon.name: "list-add"
            text: provider.add_needs_name ? "Add " + provider.label + " profile…"
                : provider.accounts.length ? "Add " + provider.label + " account…" : "Sign in to " + provider.label + "…"
            onClicked: {
                if (provider.add_needs_name) {
                    nameRow.visible = true;
                    nameField.forceActiveFocus();
                } else {
                    root.addAccount(provider.id);
                }
            }
        }

        RowLayout {
            id: nameRow
            visible: false
            Layout.fillWidth: true
            PlasmaComponents.TextField {
                id: nameField
                Layout.fillWidth: true
                placeholderText: "Profile name, e.g. work or you@example.com"
                onAccepted: createButton.clicked()
                Keys.onEscapePressed: { nameRow.visible = false; text = ""; }
            }
            PlasmaComponents.Button {
                id: createButton
                text: "Create"
                enabled: nameField.text.trim() !== ""
                onClicked: {
                    root.addAccount(section.provider.id, nameField.text.trim());
                    nameField.text = "";
                    nameRow.visible = false;
                }
            }
            PlasmaComponents.ToolButton {
                icon.name: "dialog-cancel"
                display: PlasmaComponents.AbstractButton.IconOnly
                text: "Cancel"
                onClicked: { nameRow.visible = false; nameField.text = ""; }
            }
        }
    }

    component AccountRow: PlasmaComponents.ItemDelegate {
        id: rowItem
        property var provider
        property var account
        readonly property bool manageable: provider && provider.manageable !== false
        readonly property bool clickable: manageable && (!account.active || account.launchable === true)
        readonly property bool removable: manageable && (account.removable !== undefined ? account.removable : !account.active)
        highlighted: account.active
        hoverEnabled: true
        onClicked: {
            if (clickable)
                root.action(["switch", provider.id, account.id]);
        }
        PlasmaComponents.ToolTip.text: !manageable ? "Signed in with agy (status only)"
            : account.launchable === true ? "Click to open Claude Desktop with this profile"
            : account.active ? "Active account" : "Click to switch to this account"
        PlasmaComponents.ToolTip.visible: hovered
        PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay

        contentItem: RowLayout {
            spacing: Kirigami.Units.smallSpacing

            Kirigami.Icon {
                Layout.alignment: Qt.AlignTop
                Layout.preferredWidth: Kirigami.Units.iconSizes.small
                Layout.preferredHeight: Kirigami.Units.iconSizes.small
                source: account.active ? "checkmark" : "user-identity"
                opacity: account.active ? 1 : 0.5
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                RowLayout {
                    Layout.fillWidth: true
                    PlasmaComponents.Label {
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        text: account.email
                        font.weight: account.active ? Font.Bold : Font.Normal
                    }
                    PlasmaComponents.Label {
                        visible: text !== ""
                        opacity: 0.7
                        text: [account.plan, account.has_credentials ? "" : "login missing"].filter(s => s).join(" · ")
                    }
                }

                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    visible: !account.usage || !account.usage.available
                    opacity: 0.6
                    wrapMode: Text.Wrap
                    font: Kirigami.Theme.smallFont
                    text: account.usage ? account.usage.message : "Usage not checked yet"
                }

                Repeater {
                    model: account.usage && account.usage.available ? account.usage.windows : []
                    delegate: ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0
                        PlasmaComponents.Label {
                            Layout.fillWidth: true
                            font: Kirigami.Theme.smallFont
                            text: modelData.label + "  " + Math.round(modelData.percent) + "%"
                                + (modelData.resets_in ? "  ·  resets in " + modelData.resets_in
                                   + (modelData.resets_at_local ? " (" + modelData.resets_at_local + ")" : "") : "")
                        }
                        PlasmaComponents.ProgressBar {
                            Layout.fillWidth: true
                            from: 0
                            to: 100
                            value: modelData.percent
                        }
                    }
                }
            }

            PlasmaComponents.ToolButton {
                Layout.alignment: Qt.AlignTop
                visible: rowItem.removable
                icon.name: "edit-delete-remove"
                display: PlasmaComponents.AbstractButton.IconOnly
                text: "Remove " + account.email
                PlasmaComponents.ToolTip.text: text
                PlasmaComponents.ToolTip.visible: hovered
                onClicked: root.pendingRemoval = {provider: rowItem.provider, account: rowItem.account}
            }
        }
    }
}
