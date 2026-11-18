// 問答面板（Smart_Chat 與 Smart_RAG 共用）：對話泡泡（使用者在右、助理在左）、思考中動畫、提示晶片的歡迎畫面、圓角輸入列。
// 前台不保存任何對話：資料模型半小時沒人操作就清掉，這裡的輸入框跟著清空。
import QtQuick
import QtQuick.Controls.Basic

Item {
    id: root
    property string key: "talk"
    property var qa: ({})
    property string icon: "smart_chat"
    property color accent: "#a78bfa"
    property string tagline: ""
    property string welcome: ""
    property string hint: ""
    property var chips: []
    property string sourcesLabel: "來源文件"

    function focusInput() { field.forceActiveFocus() }
    onVisibleChanged: if (visible) focusTimer.restart()
    Timer { id: focusTimer; interval: 80; onTriggered: root.focusInput() }

    Connections {
        target: backend
        function onPanelCleared(k) { if (k === root.key) field.text = "" }
    }

    Text {
        id: tag
        x: 28; y: 18
        text: root.tagline
        color: backend.palette.text_muted
        font.pixelSize: 20
    }
    Text {
        id: statusText
        x: 28
        anchors.top: tag.bottom
        anchors.topMargin: 6
        width: parent.width - 56
        text: root.qa.status || ""
        visible: text !== "" && root.qa.thinking !== true      // 思考中已經有跳動的點，不重複顯示
        height: visible ? implicitHeight : 0
        color: backend.palette.text_muted
        font.pixelSize: 24
        wrapMode: Text.WordWrap
    }

    // 對話區
    ListView {
        id: list
        visible: !(root.qa.welcome === true)
        x: 28
        anchors.top: statusText.bottom
        anchors.topMargin: 8
        anchors.bottom: inputRow.top
        anchors.bottomMargin: 12
        width: parent.width - 56
        clip: true
        spacing: 22
        model: root.qa.messages || []
        boundsBehavior: Flickable.StopAtBounds
        onCountChanged: positionTimer.restart()
        Timer { id: positionTimer; interval: 30; onTriggered: list.positionViewAtEnd() }
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        delegate: Item {
            id: row
            required property var modelData
            readonly property bool mine: modelData.role === "user"
            readonly property bool failed: modelData.role === "error"
            width: list.width - 14
            height: Math.max(bubble.height, avatar.height) + 2
            readonly property real maxTextWidth: width - 56 - 14 - 60 - 44      // 頭像 56、間距 14、對側留白 60、泡泡左右內距 44

            // 頭像：使用者是人形，助理是這個模組的圖示
            Rectangle {
                id: avatar
                width: 56; height: 56; radius: 28
                x: row.mine ? row.width - width : 0
                y: 0
                color: Qt.tint(backend.palette.card, Qt.alpha(row.failed ? backend.palette.danger : root.accent, 0.28))
                border.width: 2
                border.color: row.failed ? backend.palette.danger : root.accent
                Image {
                    anchors.centerIn: parent
                    width: 32; height: 32
                    sourceSize: Qt.size(64, 64)
                    source: backend.iconUri(row.mine ? "smart_user" : (row.failed ? "smart_alert" : root.icon),
                                            backend.palette.text, row.failed ? backend.palette.danger : root.accent)
                }
            }
            Rectangle {
                id: bubble
                x: row.mine ? avatar.x - 14 - width : avatar.width + 14
                width: Math.max(bodyText.contentWidth, sourcesFlow.visible ? sourcesFlow.implicitWidth : 0) + 44
                height: contentCol.height + 32
                radius: 22
                color: row.mine ? Qt.tint(backend.palette.card, Qt.alpha(root.accent, 0.30))
                                : (row.failed ? Qt.tint(backend.palette.card, Qt.alpha(backend.palette.danger, 0.18))
                                              : backend.palette.card)
                border.width: 1
                border.color: Qt.tint(backend.palette.border, Qt.alpha(root.accent, row.mine ? 0.7 : 0.3))
                Column {
                    id: contentCol
                    x: 22; y: 16
                    width: row.maxTextWidth
                    spacing: 12
                    Text {
                        id: bodyText
                        width: row.maxTextWidth
                        text: row.modelData.text
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: backend.palette.text
                        font.pixelSize: 28
                        lineHeight: 1.25
                    }
                    Flow {
                        id: sourcesFlow
                        visible: !row.mine && (row.modelData.sources || []).length > 0
                        width: row.maxTextWidth
                        spacing: 10
                        Text {
                            text: root.sourcesLabel + "："
                            color: backend.palette.text_muted
                            font.pixelSize: 20
                            anchors.verticalCenter: undefined
                        }
                        Repeater {
                            model: row.modelData.sources || []
                            Rectangle {
                                required property string modelData
                                height: 34
                                width: chipText.implicitWidth + 28
                                radius: 17
                                color: Qt.alpha(root.accent, 0.18)
                                border.width: 1
                                border.color: Qt.alpha(root.accent, 0.7)
                                Text {
                                    id: chipText
                                    anchors.centerIn: parent
                                    text: parent.modelData
                                    color: root.accent
                                    font.pixelSize: 20
                                }
                            }
                        }
                    }
                }
            }
        }

        // 思考中：三個跳動的點
        footer: Item {
            width: list.width
            height: root.qa.thinking === true ? 84 : 0
            visible: root.qa.thinking === true
            Rectangle {
                x: 0; y: 16
                width: 56; height: 56; radius: 28
                color: Qt.tint(backend.palette.card, Qt.alpha(root.accent, 0.28))
                border.width: 2
                border.color: root.accent
                Image {
                    anchors.centerIn: parent
                    width: 32; height: 32
                    sourceSize: Qt.size(64, 64)
                    source: backend.iconUri(root.icon, backend.palette.text, root.accent)
                }
            }
            Row {
                x: 78; y: 34
                spacing: 12
                Repeater {
                    model: 3
                    Rectangle {
                        id: dot
                        required property int index
                        width: 16; height: 16; radius: 8
                        color: root.accent
                        SequentialAnimation on opacity {
                            running: root.qa.thinking === true
                            loops: Animation.Infinite
                            PauseAnimation { duration: dot.index * 180 }
                            NumberAnimation { to: 0.25; duration: 360 }
                            NumberAnimation { to: 1.0; duration: 360 }
                            PauseAnimation { duration: (2 - dot.index) * 180 }
                        }
                    }
                }
            }
        }
    }

    // 歡迎畫面：模組圖示、標語與提示晶片（點一下就送出）
    Column {
        visible: root.qa.welcome === true
        anchors.centerIn: parent
        anchors.verticalCenterOffset: -50
        spacing: 14
        Badge {
            anchors.horizontalCenter: parent.horizontalCenter
            diameter: 132
            icon: root.icon
            accent: root.accent
        }
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: root.welcome
            color: backend.palette.text
            font.pixelSize: 40
            font.bold: true
        }
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: root.hint
            color: backend.palette.text_muted
            font.pixelSize: 24
        }
    }

    // 輸入列上方的提示晶片
    Row {
        id: chipRow
        visible: root.qa.welcome === true && root.qa.inputEnabled !== false && root.chips.length > 0
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: inputRow.top
        anchors.bottomMargin: 18
        spacing: 16
        Repeater {
            model: root.chips
            Rectangle {
                id: chip
                required property string modelData
                height: 56
                width: chipLabel.implicitWidth + 44
                radius: 28
                color: chipArea.containsMouse ? Qt.alpha(root.accent, 0.28) : backend.palette.card
                border.width: 1
                border.color: Qt.alpha(root.accent, chipArea.containsMouse ? 1.0 : 0.55)
                Behavior on color { ColorAnimation { duration: 120 } }
                Text {
                    id: chipLabel
                    anchors.centerIn: parent
                    text: chip.modelData
                    color: backend.palette.text
                    font.pixelSize: 24
                }
                MouseArea {
                    id: chipArea
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: backend.submit(root.key, chip.modelData)
                }
            }
        }
    }

    // 輸入列
    Rectangle {
        id: inputRow
        visible: root.qa.inputVisible !== false
        x: 28
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 18
        width: parent.width - 56
        height: 84
        radius: 42
        color: backend.palette.card
        border.width: field.activeFocus ? 2 : 1
        border.color: field.activeFocus ? root.accent : Qt.tint(backend.palette.border, Qt.alpha(root.accent, 0.4))
        Behavior on border.color { ColorAnimation { duration: 140 } }
        TextField {
            id: field
            anchors.left: parent.left
            anchors.right: sendButton.left
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: 26
            anchors.rightMargin: 8
            enabled: root.qa.inputEnabled !== false
            maximumLength: 300
            placeholderText: "用鍵盤輸入問題，按 Enter 送出"
            placeholderTextColor: backend.palette.text_muted
            color: backend.palette.text
            font.pixelSize: 28
            background: null
            selectByMouse: true
            Keys.onPressed: backend.touch()
            onAccepted: root.send()
        }
        Rectangle {
            id: sendButton
            width: 64; height: 64; radius: 32
            anchors.right: parent.right
            anchors.rightMargin: 10
            anchors.verticalCenter: parent.verticalCenter
            enabled: root.qa.inputEnabled !== false
            opacity: enabled ? 1.0 : 0.4
            color: sendArea.pressed ? Qt.darker(root.accent, 1.3) : root.accent
            scale: sendArea.containsMouse ? 1.08 : 1.0
            Behavior on scale { NumberAnimation { duration: 120 } }
            Image {
                anchors.centerIn: parent
                width: 34; height: 34
                sourceSize: Qt.size(68, 68)
                source: backend.iconUri("smart_send", backend.palette.on_primary, backend.palette.on_primary)
            }
            MouseArea {
                id: sendArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.send()
            }
        }
    }

    function send() {
        if (backend.submit(root.key, field.text)) field.text = ""
    }
}
