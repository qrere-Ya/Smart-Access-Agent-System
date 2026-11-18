// 內容卡片：圓角、細邊線、左上標題（圖示加文字）與霓虹底線；內容放進 content
import QtQuick

Rectangle {
    id: root
    property string title: ""
    property string icon: ""
    property color accent: backend.palette.primary
    default property alias content: body.data
    property alias body: body
    radius: 14
    color: backend.palette.card
    border.width: 1
    border.color: Qt.tint(backend.palette.border, Qt.alpha(root.accent, 0.3))

    Row {
        id: head
        visible: root.title !== ""
        x: 22; y: 14
        spacing: 10
        Image {
            width: 30; height: 30
            sourceSize: Qt.size(60, 60)
            source: root.icon === "" ? "" : backend.iconUri(root.icon, backend.palette.text, root.accent)
        }
        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: root.title
            color: backend.palette.text
            font.pixelSize: 22
            font.bold: true
        }
    }
    NeonLine {
        visible: root.title !== ""
        x: 22
        y: 52
        width: parent.width - 44
        height: 2
        colorA: root.accent
        colorB: backend.palette.accent
        scan: false
    }
    Item {
        id: body
        x: 0
        y: root.title === "" ? 0 : 62
        width: parent.width
        height: parent.height - y
    }
}
