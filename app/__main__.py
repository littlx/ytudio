"""直接运行: python -m app"""
import uvicorn

from . import config

if __name__ == "__main__":
    # 安全检查：监听非回环地址却没配 token，等于把 cookies 等凭证暴露在局域网
    if not config.is_local_only() and not config.AUTH_TOKEN:
        print("\n" + "=" * 64)
        print("⚠️  安全警告：HOST 绑定到非回环地址但未配置 AUTH_TOKEN")
        print(f"    当前 HOST={config.HOST}，局域网内任何人都能读写你的")
        print("    YouTube cookies（含登录凭证）并触发任务。")
        print("    请在 .env 设置 AUTH_TOKEN=<随机字符串> 后重启。")
        print("=" * 64 + "\n")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
