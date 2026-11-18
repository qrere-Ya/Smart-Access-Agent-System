// 霓虹漸層細線，上面有一道光點來回掃過（科技風標題底線）
import QtQuick

Item {
    id: root
    property color colorA: backend.palette.glow
    property color colorB: backend.palette.accent
    property bool scan: true
    height: 3
    clip: true

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: Qt.alpha(root.colorA, 0.0) }
            GradientStop { position: 0.08; color: root.colorA }
            GradientStop { position: 0.85; color: root.colorB }
            GradientStop { position: 1.0; color: Qt.alpha(root.colorB, 0.0) }
        }
    }
    Rectangle {
        id: spark
        visible: root.scan
        width: 220
        height: root.height
        y: 0
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "transparent" }
            GradientStop { position: 0.5; color: Qt.alpha("#ffffff", 0.9) }
            GradientStop { position: 1.0; color: "transparent" }
        }
        NumberAnimation on x {
            from: -220
            to: root.width
            duration: 3800
            loops: Animation.Infinite
            running: root.scan && root.visible
        }
    }
}
