"""
weather_server.py：內建的天氣 MCP 伺服器（stdio，官方 MCP SDK）。
工具 get_weather(city)：現在與未來三天的天氣，免費、免金鑰。

串接模組：rag.talk_tools.weather（實際查詢）；rag.mcp_client（以 MCP 協定連線並呼叫）。
"""
from mcp.server.fastmcp import FastMCP

from rag import talk_tools

server = FastMCP("smart-weather")


@server.tool()
def get_weather(city: str) -> str:
    """查某個城市現在與未來三天的天氣（城市名，例如 台北、高雄、東京）。"""
    try:
        return talk_tools.weather(city)
    except talk_tools.ToolError as e:
        return f"查詢失敗：{e}"


if __name__ == "__main__":
    server.run()
