"""
search_server.py：內建的搜尋 MCP 伺服器（stdio，官方 MCP SDK）。工具 search_news(query)（Google 新聞 RSS，最新消息）與
search_wikipedia(query)（維基百科，一般知識），都免費、免金鑰。

串接模組：rag.talk_tools.news／wikipedia（實際查詢）；rag.mcp_client（以 MCP 協定連線並呼叫）。
"""
from mcp.server.fastmcp import FastMCP

from rag import talk_tools

server = FastMCP("smart-search")


def _lines(function, query):
    try:
        lines = function(query)
    except talk_tools.ToolError as e:
        return f"查詢失敗：{e}"
    return "\n".join(lines) if lines else "沒有找到結果"


@server.tool()
def search_news(query: str) -> str:
    """搜尋最新新聞與近期消息（搜尋關鍵字）。"""
    return _lines(talk_tools.news, " ".join(str(query).split())[:100])


@server.tool()
def search_wikipedia(query: str) -> str:
    """查維基百科的一般知識（搜尋關鍵字）。"""
    return _lines(talk_tools.wikipedia, " ".join(str(query).split())[:100])


if __name__ == "__main__":
    server.run()
