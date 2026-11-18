// 簡單表格：標題列加斑馬紋資料列；columns 是 [{key, title, width(比例)}]，rows 是物件陣列，列可帶 tone 決定字色
import QtQuick

Item {
    id: root
    property var columns: []
    property var rows: []
    property int rowHeight: 40
    property int fontSize: 19
    property color accent: backend.palette.primary
    clip: true

    function toneColor(tone) {
        if (tone === "success") return backend.palette.success
        if (tone === "warning") return backend.palette.warning
        if (tone === "danger") return backend.palette.danger
        return backend.palette.text
    }
    readonly property real totalWeight: {
        var t = 0
        for (var i = 0; i < columns.length; i++) t += columns[i].width
        return t
    }

    Rectangle {
        id: head
        width: parent.width
        height: root.rowHeight
        color: backend.palette.table_head
        radius: 6
        Row {
            anchors.fill: parent
            Repeater {
                model: root.columns
                Text {
                    required property var modelData
                    width: head.width * modelData.width / root.totalWeight
                    height: head.height
                    leftPadding: 12
                    verticalAlignment: Text.AlignVCenter
                    text: modelData.title
                    color: backend.palette.text
                    font.pixelSize: root.fontSize
                    font.bold: true
                }
            }
        }
        Rectangle { width: parent.width; height: 2; color: root.accent; anchors.bottom: parent.bottom; opacity: 0.7 }
    }
    ListView {
        anchors.top: head.bottom
        anchors.bottom: parent.bottom
        width: parent.width
        clip: true
        model: root.rows
        delegate: Rectangle {
            required property int index
            required property var modelData
            width: ListView.view.width
            height: root.rowHeight
            color: index % 2 === 0 ? backend.palette.table_bg : backend.palette.table_alt
            Row {
                anchors.fill: parent
                Repeater {
                    model: root.columns
                    Text {
                        required property var modelData
                        width: parent.width * modelData.width / root.totalWeight
                        height: root.rowHeight
                        leftPadding: 12
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                        text: parent.parent.modelData[modelData.key] || ""
                        color: root.toneColor(parent.parent.modelData.tone)
                        font.pixelSize: root.fontSize
                    }
                }
            }
        }
    }
}
