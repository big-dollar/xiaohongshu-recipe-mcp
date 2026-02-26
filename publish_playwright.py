import asyncio
import os
from typing import List, Optional
from playwright.async_api import async_playwright, Page

async def save_cookies(page: Page, cookie_file: str):
    cookies = await page.context.cookies()
    import json
    with open(cookie_file, 'w', encoding='utf-8') as f:
        json.dump(cookies, f)

async def load_cookies(page: Page, cookie_file: str) -> bool:
    import json
    if os.path.exists(cookie_file):
        with open(cookie_file, 'r', encoding='utf-8') as f:
            cookies = json.load(f)
            await page.context.add_cookies(cookies)
        return True
    return False

async def login_xiaohongshu(page: Page) -> bool:
    """处理小红书登录"""
    cookie_file = "xhs_cookies.json"
    
    # 尝试加载 Cookie
    if await load_cookies(page, cookie_file):
        await page.goto("https://creator.xiaohongshu.com/creator/home")
        await page.wait_for_timeout(2000)
        # 检查是否成功登录（寻找某个登录后的元素）
        if "creator/home" in page.url or await page.locator("text=数据总览").count() > 0:
            print("使用 Cookie 登录成功！")
            return True
            
    # 如果 Cookie 无效，走扫码登录流程
    print("需要扫码登录小红书，请在弹出的浏览器中进行操作...")
    await page.goto("https://creator.xiaohongshu.com/login")
    
    # 等待用户扫码并进入主页
    try:
        # 小红书登录后可能会跳转到不同地址，放宽匹配规则并增加超时时间
        await page.wait_for_url(lambda url: "creator/home" in url or "new/home" in url, timeout=120000)
        print("扫码登录成功，保存 Cookie...")
        await save_cookies(page, cookie_file)
        return True
    except Exception as e:
        print(f"登录超时或失败: {e}")
        # 如果是因为已经登录，只是 URL 匹配不上，我们做一次额外检查
        if await page.locator("text=数据总览").count() > 0 or await page.locator("text=发布笔记").count() > 0:
             print("发现登录成功特征，强制视为登录成功")
             await save_cookies(page, cookie_file)
             return True
        return False

async def publish_with_playwright(title: str, content: str, image_paths: List[str] = [], video_path: Optional[str] = None) -> str:
    """使用 Playwright 模拟浏览器操作发布笔记"""
    if not image_paths and not video_path:
        raise ValueError("没有需要上传的图片或视频素材")
        
    async with async_playwright() as p:
        # 使用有头模式启动，方便观察和处理可能的滑块验证码
        # 优化启动速度，减少无用参数
        browser = await p.chromium.launch(
            headless=False, 
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-extensions',
                '--disable-component-extensions-with-background-pages',
                '--disable-default-apps',
                '--mute-audio',
                '--no-default-browser-check',
                '--no-first-run',
                '--disable-background-networking',
                '--disable-background-timer-throttling',
                '--disable-client-side-phishing-detection',
                '--disable-popup-blocking',
                '--disable-prompt-on-repost',
                '--disable-sync',
                '--metrics-recording-only',
                '--no-experiments',
                '--safebrowsing-disable-auto-update',
                '--password-store=basic',
                '--use-mock-keychain'
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        # 1. 登录
        if not await login_xiaohongshu(page):
            await browser.close()
            raise RuntimeError("登录失败，无法发布。")
            
        # 2. 进入发布页面
        await page.goto("https://creator.xiaohongshu.com/publish/publish")
        await page.wait_for_timeout(3000)
        
        # 3. 选择发布类型并上传文件
        if video_path:
            print(f"正在准备上传视频: {video_path}")
            
            # 点击上传视频选项卡 (如果存在)
            tab_video = page.locator('div.tab >> text="上传视频"')
            if await tab_video.count() > 0:
                await tab_video.click()
                await page.wait_for_timeout(1000)
                
            file_input = page.locator('input[type="file"]')
            await file_input.first.set_input_files(video_path)
            # 等待视频上传完成 (检测上传进度条或完成标志)
            print("等待视频上传...")
            await page.wait_for_timeout(15000) # 根据视频大小硬等待，或者更好的是查找特定元素
        elif image_paths:
             print(f"正在准备上传图片: {len(image_paths)} 张")
             # 小红书默认打开“上传视频”tab，必须先点击“上传图文”选项卡
             tab_image = page.locator('div.tab >> text="上传图文"')
             if await tab_image.count() > 0:
                 # 使用 evaluate 执行 javascript 点击，避免 playwright 的可见性检查
                 await tab_image.first.evaluate("el => el.click()")
                 print("通过 JS 点击切换到'上传图文'标签页")
                 await page.wait_for_timeout(1000)
             else:
                 # 备选选择器，根据实际页面DOM可能有所不同
                 tab_image_alt = page.locator('text="上传图文"')
                 if await tab_image_alt.count() > 0:
                     await tab_image_alt.first.evaluate("el => el.click()")
                     print("通过 JS 备选选择器点击切换到'上传图文'标签页")
                     await page.wait_for_timeout(1000)
                 else:
                     print("未找到'上传图文'标签，可能当前页面已经是图文模式")
             
             file_input = page.locator('input[type="file"]')
             if await file_input.count() > 0:
                 await file_input.first.set_input_files(image_paths)
                 print("等待图片上传...")
                 await page.wait_for_timeout(5000)
             else:
                 raise RuntimeError("未找到上传图文的 input 元素")

        # 4. 填写标题和内容
        print("填写标题和内容...")
        # 为了保证元素能稳定加载出来，稍微等一会
        await page.wait_for_timeout(3000)
        
        # 强制在前端截断标题以防 AI 不听话 (小红书标题最多 20 个字/字符)
        # Python 中的 len() 对于一个汉字或一个字母都算 1，emoji 有的算 1 有的算 2
        # 我们保守一点，直接切片到前18个字符
        safe_title = title
        if len(safe_title) > 18:
            safe_title = safe_title[:18]
        print(f"原标题: {title} | 截断后标题: {safe_title}")
        
        # 标题输入框: 尝试多种选择器
        print("查找标题输入框...")
        title_selectors = [
            'input.c-input_inner',
            'input[placeholder*="标题"]',
            '.title-input input'
        ]
        
        title_filled = False
        for selector in title_selectors:
            if await page.locator(selector).count() > 0:
                await page.locator(selector).first.fill(safe_title)
                print(f"使用选择器 {selector} 成功填写标题")
                title_filled = True
                break
                
        if not title_filled:
            print("警告：未找到合适的标题输入框！")

        # 正文输入框: 
        print("查找正文输入框...")
        # 小红书的编辑器比较复杂，通常是 #post-textarea 或者特定的 div
        content_selectors = [
            '#post-textarea',
            '.editor-content',
            '[contenteditable="true"]'
        ]
        
        content_filled = False
        for selector in content_selectors:
            if await page.locator(selector).count() > 0:
                await page.locator(selector).first.click()
                await page.wait_for_timeout(500) # 点击后等它获取焦点
                # 分段输入防止过快被拦截
                await page.keyboard.insert_text(content)
                print(f"使用选择器 {selector} 成功填写正文")
                content_filled = True
                break
                
        if not content_filled:
            print("警告：未找到合适的正文输入框！")
             
        # 给它一点时间反应文本输入
        await page.wait_for_timeout(2000)
        
        # 5. 点击发布
        print("准备点击发布...")
        publish_btn = page.locator('button.publishBtn')
        if await publish_btn.count() == 0:
            publish_btn = page.locator('button:has-text("发布")')
            
        if await publish_btn.count() > 0:
            await publish_btn.first.click()
            print("等待发布成功提示...")
            await page.wait_for_timeout(8000)
            return "Playwright 模拟点击发布完成！"
        else:
            return "未找到发布按钮，请手动点击发布。"

if __name__ == "__main__":
    pass