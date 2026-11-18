// 資源進度條：標籤、數值與一條會平滑變長的霓虹進度（70% 黃、90% 紅，由資料模型給的 tone 決定顏色）
import QtQuick

Item {
    id: root
    property string label: ""
    property var info: ({})
    height: 58

    function toneColor(t) {
        return t === "danger" ? backend.palette.danger : t === "warning" ? backend.palette.warning
                                                                         : backend.palette.success
    }
    Text {
        x: 4
        text: root.label
        color: backend.palette.text_muted
        font.pixelSize: 18
    }
    Text {
        anchors.right: parent.right
        anchors.rightMargin: 4
        text: root.info.text || "—"
        color: backend.palette.text
        font.pixelSize: 20
        font.bold: true
        font.family: "Cascadia Mono, Consolas"
    }
    Rectangle {
        id: track
        y: 32
        width: parent.width
        height: 12
        radius: 6
        color: backend.palette.table_head
        Rectangle {
            width: Math.max(12, track.width * (root.info.ratio || 0))
            height: parent.height
            radius: 6
            visible: (root.info.ratio || 0) > 0
            color: root.toneColor(root.info.tone)
            Behavior on width { NumberAnimation { duration: 500; easing.type: Easing.OutCubic } }
            Behavior on color { ColorAnimation { duration: 300 } }
        }
    }
}
