// 功能模組卡片：HUD 括號、發光徽章、名稱與一行即時預覽；每個模組有自己的顏色，滑過時亮邊放大
import QtQuick
import QtQuick.Effects

Item {
    id: root
    property var tile: ({})
    property color accent: "#22d3ee"
    signal activated()
    width: 340
    height: 250

    property bool hovered: area.containsMouse

    Rectangle {
        id: card
        anchors.fill: parent
        radius: 14
        color: backend.palette.card
        border.width: root.hovered ? 2 : 1
        border.color: root.hovered ? root.accent : Qt.tint(backend.palette.border, Qt.alpha(root.accent, 0.4))
        scale: root.hovered ? 1.03 : 1.0
        Behavior on scale { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
        Behavior on border.color { ColorAnimation { duration: 140 } }

        // 滑過時的光暈
        layer.enabled: root.hovered
        layer.effect: MultiEffect {
            shadowEnabled: true
            shadowColor: root.accent
            shadowBlur: 1.0
            shadowOpacity: 0.55
            shadowVerticalOffset: 0
        }

        // 四角 HUD 括號
        Repeater {
            model: 4
            Item {
                required property int index
                x: index % 2 === 0 ? 8 : card.width - 8 - width
                y: index < 2 ? 8 : card.height - 8 - height
                width: 26
                height: 26
                Rectangle {
                    width: parent.width; height: 3; color: root.accent
                    anchors.top: index < 2 ? parent.top : undefined
                    anchors.bottom: index < 2 ? undefined : parent.bottom
                }
                Rectangle {
                    width: 3; height: parent.height; color: root.accent
                    anchors.left: index % 2 === 0 ? parent.left : undefined
                    anchors.right: index % 2 === 0 ? undefined : parent.right
                }
            }
        }
        // 底部中間的亮條
        Rectangle {
            width: 48; height: 2; radius: 1; color: root.accent
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 2
        }

        Column {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: parent.top
            anchors.topMargin: 22
            spacing: 6
            Badge {
                anchors.horizontalCenter: parent.horizontalCenter
                diameter: 92
                icon: root.tile.icon || ""
                accent: root.accent
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                horizontalAlignment: Text.AlignHCenter
                text: (root.tile.title || "") + "\n" + (root.tile.name || "")
                color: backend.palette.text
                font.pixelSize: 24
                font.bold: true
                lineHeight: 1.05
            }
            Text {
                width: 290
                anchors.horizontalCenter: parent.horizontalCenter
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                text: root.tile.summary || ""
                color: root.tile.accent === "warning" ? backend.palette.warning
                       : root.tile.accent === "danger" ? backend.palette.danger : backend.palette.text_muted
                font.pixelSize: 18
            }
        }
    }
    MouseArea {
        id: area
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.activated()
    }
}
