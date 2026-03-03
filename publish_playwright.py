import asyncio
import os
import json
from datetime import datetime
from typing import List, Optional
from playwright.async_api import async_playwright, Page


async def save_cookies(page: Page, cookie_file: str):
    cookies = await page.context.cookies()
    with open(cookie_file, 'w', encoding='utf-8') as f:
        json.dump(cookies, f)


async def load_cookies(page: Page, cookie_file: str) -> bool:
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


async def take_screenshot(page: Page, label: str = "result") -> str:
    """截图并保存到 publish_screenshots 目录，返回截图文件路径"""
    screenshot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "publish_screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = os.path.join(screenshot_dir, f"{label}_{timestamp}.png")
    try:
        await page.screenshot(path=screenshot_path, full_page=False)
        print(f"截图已保存: {screenshot_path}")
        return screenshot_path
    except Exception as e:
        print(f"截图失败: {e}")
        return ""


async def publish_with_playwright(title: str, content: str, image_paths: List[str] = [], video_path: Optional[str] = None, cover_image_paths: List[str] = [], save_draft: bool = False) -> str:
    """使用 Playwright 模拟浏览器操作发布笔记，发布后自动截图留存。
    
    Args:
        title:             笔记标题
        content:           笔记正文
        image_paths:       图文模式下的图片本地路径列表
        video_path:        视频模式下的视频本地路径
        cover_image_paths: 视频模式下额外上传的封面图路径列表（最多3张）
    """
    if not image_paths and not video_path:
        raise ValueError("没有需要上传的图片或视频素材")
        
    async with async_playwright() as p:
        # 使用有头模式启动，方便观察和处理可能的滑块验证码
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
        screenshot_path = ""
        
        try:
            # 1. 登录
            if not await login_xiaohongshu(page):
                await take_screenshot(page, "login_failed")
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
                # 等待视频上传完成
                print("等待视频上传...")
                
                # 轮询检查上传进度或者等待一个较长的固定时间
                # 小红书上传视频时，界面上会有类似“重新上传”的按钮出现，表示上传完成
                # 我们这里加长默认等待时间，最大等待 2 分钟 (120秒)
                try:
                    await page.wait_for_selector('text="重新上传"', timeout=120000)
                    print("✅ 视频上传成功。")
                    await page.wait_for_timeout(2000) # 缓冲一下
                except Exception:
                    print("⚠️ 等待视频上传完成标志超时，可能还在上传或页面变化。再额外等待 15 秒...")
                    await page.wait_for_timeout(15000)

                # ── 封面图上传（方案C）────────────────────────────────────────
                if cover_image_paths:
                    print(f"尝试上传 {len(cover_image_paths)} 张封面图...")
                    cover_uploaded = False

                    # 优先策略：寻找专用封面图上传区域（class 含 cover 的 file input）
                    cover_selectors = [
                        '[class*="cover"] input[type="file"]',
                        '[class*="Cover"] input[type="file"]',
                        '.cover-image input[type="file"]',
                        '.upload-cover input[type="file"]',
                    ]
                    for selector in cover_selectors:
                        cover_input = page.locator(selector)
                        if await cover_input.count() > 0:
                            await cover_input.first.set_input_files(cover_image_paths[0])
                            print(f"✅ 使用选择器 [{selector}] 成功上传封面图")
                            cover_uploaded = True
                            await page.wait_for_timeout(2000)
                            break

                    # 备选策略：页面上第二个 file input（第一个已被视频占用）
                    if not cover_uploaded:
                        all_inputs = page.locator('input[type="file"]')
                        input_count = await all_inputs.count()
                        if input_count >= 2:
                            await all_inputs.nth(1).set_input_files(cover_image_paths[0])
                            print("✅ 通过第二个 file input 成功上传封面图")
                            cover_uploaded = True
                            await page.wait_for_timeout(2000)

                    if not cover_uploaded:
                        print("⚠️ 未找到封面图上传入口，跳过封面图（不影响视频发布）")
                # ─────────────────────────────────────────────────────────────
            elif image_paths:
                print(f"正在准备上传图片: {len(image_paths)} 张")
                # 小红书默认打开"上传视频"tab，必须先点击"上传图文"选项卡
                tab_image = page.locator('div.tab >> text="上传图文"')
                if await tab_image.count() > 0:
                    # 使用 evaluate 执行 javascript 点击，避免 playwright 的可见性检查
                    await tab_image.first.evaluate("el => el.click()")
                    print("通过 JS 点击切换到'上传图文'标签页")
                    await page.wait_for_timeout(1000)
                else:
                    # 备选选择器
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
            await page.wait_for_timeout(3000)
            
            # 强制截断标题，保守切到前 18 个字符（小红书标题最多 20 字）
            safe_title = title[:18] if len(title) > 18 else title
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

            # 正文输入框
            print("查找正文输入框...")
            content_selectors = [
                '#post-textarea',
                '.editor-content',
                '[contenteditable="true"]'
            ]
            
            content_filled = False
            for selector in content_selectors:
                if await page.locator(selector).count() > 0:
                    await page.locator(selector).first.click()
                    await page.wait_for_timeout(500)
                    await page.keyboard.insert_text(content)
                    await page.keyboard.press("Enter")
                    print(f"使用选择器 {selector} 成功填写正文并追加换行")
                    content_filled = True
                    break
                    
            if not content_filled:
                print("警告：未找到合适的正文输入框！")
             
            await page.wait_for_timeout(2000)

            # --- 发布前截图留存 ---
            screenshot_path = await take_screenshot(page, "before_publish")
            
            # 5. 点击发布或暂存
            if save_draft:
                print("准备点击暂存离开...")
                publish_btn = page.locator('button:has-text("暂存离开"), button:has-text("存草稿")')
            else:
                print("准备点击发布...")
                publish_btn = page.locator('button.publishBtn')
                if await publish_btn.count() == 0:
                    publish_btn = page.locator('button:has-text("发布")')
                
            if await publish_btn.count() > 0:
                await publish_btn.first.click()
                print("等待操作完成提示...")
                await page.wait_for_timeout(8000)

                # --- 操作后截图留存，用于确认结果 ---
                screenshot_path = await take_screenshot(page, "after_publish" if not save_draft else "after_save_draft")

                if save_draft:
                    print("暂存成功！页面保持打开 300 秒，供您进入草稿箱修改和发布...")
                    try:
                        # 循环等待，每 10 秒检查一次浏览器状态，总计 300 秒
                        for _ in range(30):
                            if page.is_closed():
                                print("页面已关闭，结束等待。")
                                break
                            await page.wait_for_timeout(10000)
                    except Exception as e:
                        print(f"等待被中断或发生异常: {e}")

                action_name = "发布" if not save_draft else "暂存草稿"
                result_msg = f"Playwright 模拟点击{action_name}完成！"
                if screenshot_path:
                    result_msg += f"\n📸 {action_name}结果截图已保存至: {screenshot_path}"
                return result_msg
            else:
                # 未找到按钮也截图，方便排查
                screenshot_path = await take_screenshot(page, "no_publish_btn")
                action_name = "发布" if not save_draft else "暂存离开"
                result_msg = f"未找到{action_name}按钮，请手动点击操作。"
                if screenshot_path:
                    result_msg += f"\n📸 当前页面截图已保存至: {screenshot_path}"
                return result_msg

        except Exception as e:
            # 出错时截图，方便排查
            await take_screenshot(page, "error")
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    pass