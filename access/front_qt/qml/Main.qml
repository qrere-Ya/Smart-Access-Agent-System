// 前台儀表板（PySide6 + QML）：1920×1080 的舞台，視窗多大都等比縮放置中。
// 資料全部來自 Python 的 backend（access.front_qt.model.DashboardModel）；這裡只有畫面與動畫。
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Effects

ApplicationWindow {
    id: win
    visible: true
    width: 1600
    height: 900
    minimumWidth: 800
    minimumHeight: 450
    title: "Smart Access 前台"
    color: backend.palette.bg

    readonly property var moduleTitles: ({
        "talk": "Smart_Chat 一般對話", "chat": "Smart_RAG 知識庫問答", "news": "", "greetings": "Smart_Greet 剛剛打卡",
        "status": "Smart_Status 系統狀態"})
    readonly property var moduleIcons: ({"talk": "smart_chat", "chat": "smart_rag", "news": "smart_news",
                                         "greetings": "smart_greet", "status": "smart_status"})
    readonly property var moduleTokens: ({"talk": "mod_chat", "chat": "mod_rag", "news": "mod_news",
                                          "greetings": "mod_greet", "status": "mod_status"})
    function moduleColor(key) { return backend.palette[moduleTokens[key]] }
    function toneColor(t) {
        return t === "success" ? backend.palette.success : t === "warning" ? backend.palette.warning
             : t === "danger" ? backend.palette.danger : backend.palette.secondary
    }
    function titleOf(key) {
        return key === "news" ? ((backend.news && backend.news.heading) || "今日科技早報") : moduleTitles[key]
    }

    Shortcut { sequence: "F11"; onActivated: backend.toggleFullscreen() }
    Shortcut { sequence: "Escape"; onActivated: backend.leaveFullscreen() }

    Behavior on color { ColorAnimation { duration: 250 } }

    // 背景的點陣紋理
    Canvas {
        id: dots
        anchors.fill: parent
        opacity: 0.5
        onPaint: {
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)
            ctx.fillStyle = backend.palette.grid
            for (var x = 20; x < width; x += 40)
                for (var y = 20; y < height; y += 40)
                    ctx.fillRect(x, y, 2, 2)
        }
        Connections { target: backend; function onUiChanged() { dots.requestPaint() } }
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
    }

    Item {
        id: stage
        width: 1920
        height: 1080
        anchors.centerIn: parent
        scale: Math.min(win.width / 1920, win.height / 1080)
        transformOrigin: Item.Center

        // ───────── 標題列 ─────────
        Item {
            id: top
            x: 48; y: 18
            width: parent.width - 96
            height: 56
            Row {
                spacing: 12
                anchors.verticalCenter: parent.verticalCenter
                Image {
                    width: 44; height: 44
                    sourceSize: Qt.size(88, 88)
                    source: backend.iconUri("smart_access", backend.palette.primary, backend.palette.accent)
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Smart Access 前台"
                    color: backend.palette.primary
                    font.pixelSize: 34
                    font.bold: true
                }
                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    height: 38
                    width: linkText.width + 40
                    radius: 19
                    color: win.toneColor(backend.link.tone)
                    Row {
                        anchors.centerIn: parent
                        spacing: 8
                        Rectangle {
                            width: 10; height: 10; radius: 5
                            anchors.verticalCenter: parent.verticalCenter
                            color: backend.palette.on_status
                            SequentialAnimation on opacity {
                                loops: Animation.Infinite
                                NumberAnimation { to: 0.25; duration: 900 }
                                NumberAnimation { to: 1.0; duration: 900 }
                            }
                        }
                        Text {
                            id: linkText
                            text: backend.link.text || ""
                            color: backend.palette.on_status
                            font.pixelSize: 18
                            font.bold: true
                        }
                    }
                }
            }
            Row {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: 14
                // 啟動人臉辨識
                Rectangle {
                    visible: backend.recognitionState.visible === true
                    height: 42
                    width: recText.width + 44
                    radius: 21
                    property bool on: backend.recognitionState.enabled === true
                    color: on ? (recArea.containsMouse ? backend.palette.primary_hover : backend.palette.primary)
                              : "transparent"
                    border.width: on ? 0 : 1
                    border.color: win.toneColor(backend.recognitionState.tone)
                    Text {
                        id: recText
                        anchors.centerIn: parent
                        text: (parent.on ? "▶ " : "") + (backend.recognitionState.text || "")
                        color: parent.on ? backend.palette.on_primary : win.toneColor(backend.recognitionState.tone)
                        font.pixelSize: 17
                        font.bold: true
                    }
                    MouseArea {
                        id: recArea
                        anchors.fill: parent
                        hoverEnabled: true
                        enabled: parent.on
                        cursorShape: Qt.PointingHandCursor
                        onClicked: backend.startRecognition()
                    }
                }
                Rectangle {
                    height: 42; width: 170; radius: 21
                    color: themeArea.containsMouse ? backend.palette.secondary_hover : backend.palette.secondary
                    Text {
                        anchors.centerIn: parent
                        text: backend.theme === "frosted" ? "🌙 深色藍" : "☀ 半透明白"
                        color: backend.palette.on_secondary
                        font.pixelSize: 17
                    }
                    MouseArea { id: themeArea; anchors.fill: parent; hoverEnabled: true; onClicked: backend.toggleTheme() }
                }
                Rectangle {
                    height: 42; width: 190; radius: 21
                    color: fsArea.containsMouse ? backend.palette.secondary_hover : backend.palette.secondary
                    Text {
                        anchors.centerIn: parent
                        text: backend.fullscreen ? "⛶ 離開全螢幕" : "⛶ 全螢幕 (F11)"
                        color: backend.palette.on_secondary
                        font.pixelSize: 17
                    }
                    MouseArea { id: fsArea; anchors.fill: parent; hoverEnabled: true; onClicked: backend.toggleFullscreen() }
                }
            }
        }
        NeonLine { id: topLine; x: 48; y: 84; width: parent.width - 96; height: 3 }

        // ───────── 警示橫幅 ─────────
        Rectangle {
            id: alertBar
            visible: !!backend.alert && (backend.alert.title || "") !== ""      // 沒有警示時 alert 是空值，不能用 !== null 判斷（會讓空橫幅一直顯示）
            x: 48; y: 96
            width: parent.width - 96
            height: 64
            radius: 14
            color: backend.palette.danger
            Row {
                anchors.verticalCenter: parent.verticalCenter
                x: 30
                spacing: 26
                Image {                                       // 不用 ⚠ 表情符號：部分機器的表情字型會讓同一行的中文跟著畫不出來
                    anchors.verticalCenter: parent.verticalCenter
                    width: 40; height: 40
                    sourceSize: Qt.size(80, 80)
                    source: backend.iconUri("smart_alert", backend.palette.on_danger, backend.palette.on_danger)
                }
                Text {
                    text: backend.alert ? backend.alert.title : ""
                    color: backend.palette.on_danger
                    font.pixelSize: 34
                    font.bold: true
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: backend.alert ? backend.alert.detail : ""
                    color: backend.palette.on_danger
                    font.pixelSize: 24
                }
            }
            SequentialAnimation on opacity {
                running: alertBar.visible
                loops: Animation.Infinite
                NumberAnimation { to: 0.78; duration: 700 }
                NumberAnimation { to: 1.0; duration: 700 }
            }
        }

        // ───────── 舞台：主畫面或模組 ─────────
        Item {
            id: content
            x: 36
            y: alertBar.visible ? 172 : 100
            width: parent.width - 72
            height: parent.height - y - 40
            Behavior on y { NumberAnimation { duration: 200 } }

            // 主畫面
            Item {
                id: home
                anchors.fill: parent
                opacity: backend.current === "" ? 1 : 0
                visible: opacity > 0.01
                Behavior on opacity { NumberAnimation { duration: 220 } }
                transform: Translate { y: backend.current === "" ? 0 : -14; Behavior on y { NumberAnimation { duration: 220 } } }

                Row {
                    x: 12
                    spacing: 10
                    Image {
                        width: 30; height: 30
                        sourceSize: Qt.size(60, 60)
                        source: backend.iconUri("smart_modules", backend.palette.text, backend.palette.primary)
                    }
                    Text { text: "功能模組"; color: backend.palette.text; font.pixelSize: 26; font.bold: true }
                }
                NeonLine { x: 12; y: 42; width: parent.width - 24; height: 3; scan: false }

                Row {
                    id: tiles
                    x: 4; y: 58
                    spacing: 16
                    Repeater {
                        model: backend.tiles
                        Tile {
                            required property var modelData
                            tile: modelData
                            accent: backend.palette[modelData.token]
                            onActivated: backend.openModule(modelData.key)
                        }
                    }
                }

                Row {
                    x: 0
                    y: 318
                    spacing: 24
                    Panel {
                        width: (home.width - 24) / 2
                        height: 238
                        title: "最近打卡"
                        icon: "smart_greet"
                        accent: backend.palette.mod_greet
                        DataTable {
                            x: 18; y: 4
                            width: parent.width - 36
                            height: parent.height - 12
                            rowHeight: 34
                            fontSize: 17
                            accent: backend.palette.mod_greet
                            columns: [{"key": "time", "title": "時間", "width": 90},
                                      {"key": "name", "title": "姓名", "width": 260},
                                      {"key": "greeting", "title": "問候", "width": 120}]
                            rows: backend.greetings.slice(0, 4)
                        }
                    }
                    Panel {
                        width: (home.width - 24) / 2
                        height: 238
                        title: "連線與辨識"
                        icon: "smart_status"
                        accent: backend.palette.mod_status
                        DataTable {
                            x: 18; y: 4
                            width: parent.width - 36
                            height: parent.height - 12
                            rowHeight: 34
                            fontSize: 17
                            accent: backend.palette.mod_status
                            columns: [{"key": "item", "title": "項目", "width": 180},
                                      {"key": "state", "title": "狀態", "width": 360}]
                            rows: backend.status
                        }
                    }
                }

                Panel {
                    x: 0
                    y: 566
                    width: home.width
                    height: 190
                    title: "資源狀態監控"
                    icon: "smart_monitor"
                    accent: backend.palette.mod_monitor
                    Row {
                        x: 22; y: 2
                        spacing: 40
                        Column {
                            width: (home.width - 44 - 40) / 2
                            spacing: 2
                            Meter { width: parent.width; label: "CPU"; info: backend.resources.cpu || ({}) }
                            Meter { width: parent.width; label: "GPU"; info: backend.resources.gpu || ({}) }
                        }
                        Column {
                            width: (home.width - 44 - 40) / 2
                            spacing: 2
                            Meter { width: parent.width; label: "記憶體 RAM"; info: backend.resources.ram || ({}) }
                            Item {
                                width: parent.width; height: 58
                                Text {
                                    x: 4; anchors.verticalCenter: parent.verticalCenter
                                    text: backend.resources.down || ""
                                    color: backend.palette.text
                                    font.pixelSize: 22
                                    font.family: "Cascadia Mono, Consolas"
                                }
                                Text {
                                    anchors.right: parent.right; anchors.rightMargin: 4
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: backend.resources.up || ""
                                    color: backend.palette.text
                                    font.pixelSize: 22
                                    font.family: "Cascadia Mono, Consolas"
                                }
                            }
                        }
                    }
                }

                // 日期與大時鐘
                Row {
                    anchors.horizontalCenter: parent.horizontalCenter
                    y: 762
                    spacing: 30
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: (backend.clock.date || "")
                        color: backend.palette.text_muted
                        font.pixelSize: 28
                    }
                    Text {
                        id: bigClock
                        text: backend.clock.time || ""
                        color: backend.palette.primary
                        font.pixelSize: 76
                        font.family: "Cascadia Mono, Consolas"
                        layer.enabled: true
                        layer.effect: MultiEffect {
                            shadowEnabled: true
                            shadowColor: backend.palette.primary
                            shadowBlur: 1.0
                            shadowOpacity: 0.85
                            shadowVerticalOffset: 0
                        }
                    }
                }
            }

            // 模組全版畫面
            Item {
                id: detail
                anchors.fill: parent
                opacity: backend.current === "" ? 0 : 1
                visible: opacity > 0.01
                Behavior on opacity { NumberAnimation { duration: 220 } }
                transform: Translate { y: backend.current === "" ? 14 : 0; Behavior on y { NumberAnimation { duration: 220 } } }

                Item {
                    id: bar
                    x: 12; y: 6
                    width: parent.width - 24
                    height: 56
                    Rectangle {
                        id: backBtn
                        width: 160; height: 52; radius: 26
                        color: backArea.containsMouse ? backend.palette.secondary_hover : backend.palette.secondary
                        border.width: 1
                        border.color: win.moduleColor(backend.current || "talk")
                        Text {
                            anchors.centerIn: parent
                            text: "←  返回"
                            color: backend.palette.on_secondary
                            font.pixelSize: 24
                            font.bold: true
                        }
                        MouseArea { id: backArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: backend.goHome() }
                    }
                    Image {
                        x: 188
                        anchors.verticalCenter: parent.verticalCenter
                        width: 40; height: 40
                        sourceSize: Qt.size(80, 80)
                        source: backend.current === "" ? "" : backend.iconUri(win.moduleIcons[backend.current],
                                    backend.palette.text, win.moduleColor(backend.current))
                    }
                    Text {
                        x: 240
                        anchors.verticalCenter: parent.verticalCenter
                        text: backend.current === "" ? "" : win.titleOf(backend.current)
                        color: backend.current === "" ? backend.palette.text : win.moduleColor(backend.current)
                        font.pixelSize: 34
                        font.bold: true
                    }
                    Text {
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        text: backend.current === "news" ? (backend.news.pageText || "") : ""
                        color: backend.palette.text_muted
                        font.pixelSize: 22
                    }
                }

                Panel {
                    id: body
                    x: 12
                    y: 70
                    width: parent.width - 24
                    height: parent.height - 78
                    accent: backend.current === "" ? backend.palette.primary : win.moduleColor(backend.current)

                    QAPanel {
                        anchors.fill: parent
                        visible: backend.current === "talk"
                        key: "talk"
                        qa: backend.qaTalk
                        icon: "smart_chat"
                        accent: backend.palette.mod_chat
                        tagline: "一般對話　·　可查天氣、上網搜尋　·　不查公司文件與資料　·　沒人操作半小時自動重置"
                        chips: ["台北明天天氣如何？", "台積電最新消息", "把這句話翻成英文", "用一句話介紹你自己"]
                        sourcesLabel: "使用工具"
                        welcome: "想聊什麼？"
                        hint: "用鍵盤輸入問題，例如：幫我把這句話翻成英文"
                    }
                    QAPanel {
                        anchors.fill: parent
                        visible: backend.current === "chat"
                        key: "chat"
                        qa: backend.qaChat
                        icon: "smart_rag"
                        accent: backend.palette.mod_rag
                        tagline: "只查公司文件　·　沒人操作半小時自動重置"
                        chips: ["特休有幾天？", "加班費怎麼算？", "請病假要準備什麼？"]
                        sourcesLabel: "來源文件"
                        welcome: "有什麼想問公司文件？"
                        hint: "用鍵盤輸入問題，例如：特休有幾天？加班費怎麼算？"
                    }
                    // 科技早報
                    Item {
                        anchors.fill: parent
                        visible: backend.current === "news"
                        Text {
                            visible: backend.news.state !== "ok"
                            x: 28; y: 30
                            width: parent.width - 56
                            wrapMode: Text.WordWrap
                            text: backend.news.message || ""
                            color: backend.palette.text_muted
                            font.pixelSize: 24
                        }
                        Column {
                            x: 22; y: 14
                            width: parent.width - 44
                            spacing: 14
                            Repeater {
                                model: backend.news.cards || []
                                Rectangle {
                                    required property int index
                                    required property var modelData
                                    width: parent.width
                                    height: Math.max(newsText.height + 30, modelData.qr ? 190 : 0)
                                    radius: 12
                                    color: backend.palette.bg
                                    border.width: 1
                                    border.color: Qt.tint(backend.palette.border, Qt.alpha(backend.palette.mod_news, 0.3))
                                    Column {
                                        id: newsText
                                        x: 22; y: 14
                                        width: parent.width - 44 - (modelData.qr ? 200 : 0)
                                        spacing: 6
                                        Row {
                                            spacing: 14
                                            Rectangle {
                                                height: 28; width: cat.width + 20; radius: 14
                                                color: backend.palette.mod_news
                                                Text { id: cat; anchors.centerIn: parent; text: modelData.category; color: backend.palette.on_status; font.pixelSize: 16; font.bold: true }
                                            }
                                            Text { text: modelData.source; color: backend.palette.text_muted; font.pixelSize: 16; anchors.verticalCenter: parent.verticalCenter }
                                        }
                                        Text { width: parent.width; wrapMode: Text.WordWrap; text: (index + 1) + ". " + modelData.title; color: backend.palette.text; font.pixelSize: 24; font.bold: true }
                                        Text { width: parent.width; wrapMode: Text.WordWrap; text: modelData.what; color: backend.palette.text; font.pixelSize: 19 }
                                        Text { width: parent.width; wrapMode: Text.WordWrap; text: "為什麼重要：" + modelData.why; color: backend.palette.text_muted; font.pixelSize: 17 }
                                        Text { width: parent.width; wrapMode: Text.WordWrap; text: "可以怎麼用：" + modelData.how; color: backend.palette.text_muted; font.pixelSize: 17 }
                                    }
                                    Column {
                                        visible: modelData.qr !== ""
                                        anchors.right: parent.right
                                        anchors.rightMargin: 18
                                        anchors.verticalCenter: parent.verticalCenter
                                        spacing: 4
                                        Image { width: 150; height: 150; source: modelData.qr; smooth: false }
                                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: "手機掃描看原文"; color: backend.palette.text_muted; font.pixelSize: 14 }
                                    }
                                }
                            }
                        }
                    }
                    // 剛剛打卡
                    DataTable {
                        visible: backend.current === "greetings"
                        x: 22; y: 18
                        width: parent.width - 44
                        height: parent.height - 36
                        rowHeight: 46
                        fontSize: 22
                        accent: backend.palette.mod_greet
                        columns: [{"key": "time", "title": "時間", "width": 90},
                                  {"key": "name", "title": "姓名", "width": 260},
                                  {"key": "greeting", "title": "問候", "width": 120}]
                        rows: backend.greetings
                    }
                    // 系統狀態
                    DataTable {
                        visible: backend.current === "status"
                        x: 22; y: 18
                        width: parent.width - 44
                        height: parent.height - 36
                        rowHeight: 50
                        fontSize: 24
                        accent: backend.palette.mod_status
                        columns: [{"key": "item", "title": "項目", "width": 180},
                                  {"key": "state", "title": "狀態", "width": 360}]
                        rows: backend.status
                    }
                }
            }
        }

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            y: parent.height - 34
            text: "Smart Access 前台　點上方模組進入；在模組畫面沒人操作會自動回到這一頁"
            color: backend.palette.text_muted
            font.pixelSize: 16
        }
    }
}
