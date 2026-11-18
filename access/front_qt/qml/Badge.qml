// 圓形發光徽章：柔光外圈、旋轉的霓虹弧線、半透明圓底，中間放 SVG 雙色圖示（顏色取自模組色）
import QtQuick
import QtQuick.Effects
import QtQuick.Shapes

Item {
    id: root
    property string icon: ""
    property color accent: "#22d3ee"
    property int diameter: 96
    property bool spinning: true
    width: diameter
    height: diameter

    // 外圈柔光
    Rectangle {
        id: halo
        anchors.centerIn: parent
        width: root.diameter
        height: root.diameter
        radius: width / 2
        color: Qt.alpha(root.accent, 0.35)
        visible: false
    }
    MultiEffect {
        anchors.fill: halo
        source: halo
        blurEnabled: true
        blur: 1.0
        blurMax: 48
        scale: 1.25
        opacity: 0.9
    }
    // 圓底
    Rectangle {
        anchors.centerIn: parent
        width: root.diameter - 8
        height: width
        radius: width / 2
        color: Qt.tint(backend.palette.card, Qt.alpha(root.accent, 0.20))
        border.width: 2
        border.color: root.accent
    }
    // 旋轉的霓虹弧線
    Shape {
        id: ring
        anchors.centerIn: parent
        width: root.diameter
        height: root.diameter
        layer.enabled: true
        layer.samples: 4
        ShapePath {
            strokeColor: Qt.lighter(root.accent, 1.4)
            strokeWidth: 3
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            PathAngleArc {
                centerX: root.diameter / 2
                centerY: root.diameter / 2
                radiusX: root.diameter / 2 - 1
                radiusY: root.diameter / 2 - 1
                startAngle: -30
                sweepAngle: 70
            }
        }
        RotationAnimator on rotation {
            from: 0
            to: 360
            duration: 5200
            loops: Animation.Infinite
            running: root.spinning && root.visible
        }
    }
    Image {
        anchors.centerIn: parent
        width: root.diameter * 0.52
        height: width
        sourceSize: Qt.size(width * 2, height * 2)
        source: root.icon === "" ? "" : backend.iconUri(root.icon, backend.palette.text, root.accent)
        smooth: true
    }
}
