// 問答面板（Smart_Chat 與 Smart_RAG 共用）：一行說明、狀態、回答區（純文字）或歡迎畫面、圓角輸入列。
// 前台不保存任何對話：離開或閒置時由資料模型清掉，這裡的輸入框跟著清空。
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
        anchors.topMargin: 8
        width: parent.width - 56
        text: root.qa.status || ""
        color: backend.palette.text_muted
        font.pixelSize: 26
        wrapMode: Text.WordWrap
    }

    // 回答區
    Rectangle {
        id: answerBox
        visible: !(root.qa.welcome === true)
        x: 28
        anchors.top: statusText.bottom
        anchors.topMargin: 6
        anchors.bottom: inputRow.top
        anchors.bottomMargin: 12
        width: parent.width - 56
        radius: 20
        color: backend.palette.bg
        border.width: 1
        border.color: Qt.tint(backend.palette.border, Qt.alpha(root.accent, 0.35))
        ScrollView {
            anchors.fill: parent
            anchors.margins: 18
            clip: true
            TextArea {
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                text: root.qa.text || ""
                color: backend.palette.text
                font.pixelSize: 28
                background: null
                padding: 4
            }
        }
    }
    // 歡迎畫面
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
