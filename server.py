import asyncio
import os
import re
import tempfile
import uuid
import sys
import subprocess
import threading
from typing import List, Dict, Any, Optional

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from publish_playwright import publish_with_playwright
import yt_dlp

# 加载环境变量
load_dotenv()

# 初始化服务器
server = Server("xiaohongshu-recipe")

# 配置 OpenAI 客户端 (兼容自定义 API)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-3.5-turbo")

class RecipeData(BaseModel):
    title: str = Field(description="食谱标题")
    ingredients: List[str] = Field(description="食材列表")
    steps: List[str] = Field(description="制作步骤")
    image_urls: List[str] = Field(description="图片链接列表")
    video_url: Optional[str] = Field(default=None, description="视频链接")

async def extract_recipe_from_url(url: str) -> RecipeData:
    """从任意网页提取食谱内容和图片"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }
    
    # 尝试使用 httpx 抓取
    html_content = ""
    try:
        async with httpx.AsyncClient(follow_redirects=True, http2=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            html_content = response.text
    except Exception as e:
        print(f"HTTP 请求失败 ({e})，尝试使用 Playwright 抓取...")
        # 如果 httpx 失败 (比如遇到 Cloudflare 或 403)，回退到 playwright
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=headers["User-Agent"])
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            html_content = await page.content()
            await browser.close()
            
    soup = BeautifulSoup(html_content, 'html.parser')

    # 提取标题 (尝试几种常见的标题标签)
    title = ""
    if soup.title:
        title = soup.title.string.strip()
    if not title:
        h1 = soup.find('h1')
        if h1:
            title = h1.text.strip()
            
    # 尝试寻找主要的食谱内容区域，以避免抓取到侧边栏或推荐菜谱的图片
    main_content = soup
    # 常见的正文容器特征
    content_selectors = [
        'article',
        'main',
        '.recipe-content',
        '.post-content',
        '.entry-content',
        '#recipe-block',
        '[class*="recipe"]',
        '[class*="content"]'
    ]
    
    for selector in content_selectors:
        found = soup.select_one(selector)
        if found:
            main_content = found
            break

    # 提取所有文本以供 AI 解析
    # 移除脚本和样式
    for script in main_content(["script", "style", "nav", "footer", "header", "aside"]):
        script.extract()
    text = main_content.get_text(separator='\n', strip=True)
    
    # 尝试通过 BeautifulSoup 寻找视频链接，如果 yt-dlp 失败
    video_url = None
    for video in main_content.find_all('video'):
         source = video.find('source')
         if source and source.get('src'):
             video_url = source.get('src')
             break
         elif video.get('src'):
             video_url = video.get('src')
             break
             
    if not video_url:
        # 特别处理某些常见网站的视频标签或 data 属性
        for div in main_content.find_all(attrs={'data-video-url': True}):
            video_url = div.get('data-video-url')
            break
            
        # 搜索 script 标签里的 .mp4 链接
        if not video_url:
            for script in main_content.find_all('script'):
                if script.string and '.mp4' in script.string:
                    import re
                    match = re.search(r'https?://[^\s\'"]+\.mp4[^\s\'"]*', script.string)
                    if match:
                        video_url = match.group(0)
                        break
    
    # 提取图片 URL
    images = []
    
    # 一些用来过滤非正文图片的特征关键词
    exclude_classes = ['sidebar', 'widget', 'related', 'recommended', 'footer', 'nav', 'author', 'promo']
    
    # 获取原始的所有 img 标签，因为 main_content 可能切得太狠了
    for img in main_content.find_all('img') + soup.find_all('img', class_='featured-image'):
        # 检查图片是否在不该在的地方
        skip = False
        # 如果是特色大图，不要跳过
        if 'featured-image' not in img.get('class', []):
            for parent in img.parents:
                if parent.name in ['aside', 'footer', 'nav']:
                    skip = True
                    break
                class_str = " ".join(parent.get('class', []))
                if any(exc in class_str.lower() for exc in exclude_classes):
                    skip = True
                    break
        
        if skip:
            continue
            
        src = img.get('data-lazy-src') or img.get('src') or img.get('data-src') 
        if src and not src.startswith('data:'):
            # 处理相对路径
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                from urllib.parse import urlparse
                parsed_url = urlparse(url)
                src = f"{parsed_url.scheme}://{parsed_url.netloc}{src}"
            
            # 简单过滤：忽略太小的图标或者 base64
            if src.startswith('http') and not any(skip_word in src.lower() for skip_word in ['icon', 'logo', 'avatar', 'gif', 'svg', 'thumb', 'small', '150x150', '300x300']):
                # 如果 URL 中有查询参数控制大小（比如 wp 的图像），尽量保留原图
                import re
                src = re.sub(r'-\d+x\d+\.(jpg|jpeg|png)$', r'.\1', src, flags=re.IGNORECASE)
                if src not in images:
                    images.append(src)
    
    # 使用 AI 解析网页文本，提取结构化的食谱数据
    ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    prompt = f"""
请从以下网页文本中提取食谱信息，并翻译为中文。
如果文本中不包含食谱，请尽力提取主要内容作为步骤。

网页文本：
{text[:4000]} # 截断以避免超出 token 限制

请返回 JSON 格式，包含以下字段：
- ingredients: 字符串数组，包含所需食材的中文翻译
- steps: 字符串数组，包含制作步骤的中文翻译
"""
    completion = await ai_client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": "你是一个专业的食谱信息提取助手，只返回符合格式的 JSON。"},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"}
    )
    
    import json
    content = completion.choices[0].message.content
    if content:
       extracted_data = json.loads(content)
    else:
       extracted_data = {"ingredients": [], "steps": []}
    
    # 尝试通过 yt-dlp 提取视频 URL
    # YouTube / Vimeo 等专业视频站需要特殊处理
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'ignoreerrors': True,
        'no_color': True,
        # 关闭浏览器 cookie 窃取，因为它在 Windows 上容易引起讨厌但无害的红字报错
        # 我们用一个 dummy 选项让它不要把错误打到 stderr 搞脏屏幕
        'logger': type('DummyLogger', (object,), {'debug': lambda s: None, 'warning': lambda s: None, 'error': lambda s: None})(),
    }
    if not video_url:
        try:
             with yt_dlp.YoutubeDL(params=ydl_opts) as ydl: # type: ignore
                 info = ydl.extract_info(url, download=False)
                 if info:
                     video_url = info.get('url')
                     # 如果是嵌套在某些页面中的视频，可能需要取第一个格式
                     formats = info.get('formats')
                     if video_url is None and formats:
                         for f in reversed(formats):
                             if f.get('url') and f.get('vcodec') != 'none':
                                 video_url = f.get('url')
                                 break
        except Exception as e:
             print(f"yt-dlp 提取视频 URL 失败: {e}")

    return RecipeData(
        title=title,
        ingredients=extracted_data.get('ingredients', []),
        steps=extracted_data.get('steps', []),
        image_urls=list(set(images))[:9], # 小红书最多 9 张图
        video_url=video_url
    )

async def generate_xiaohongshu_post(recipe: RecipeData) -> Dict[str, str]:
    """根据食谱数据生成小红书风格的笔记"""
    ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    
    prompt = f"""
请根据以下提取的食谱信息，为我生成一篇【有故事性、实用性】的爆款小红书美食笔记。

【核心要求】
1. 标题（绝对不能超过18个字符，包含emoji在内）：必须极具吸睛效果，切中痛点或带有夸张吸引力（例如：绝了！被全家夸上天的神仙XXX）。
2. 开篇引入：用 1-2 句话讲述一个引起共鸣的小故事或日常场景（例如：周末不知道吃什么？/ 闺蜜尝了一口直接找我要配方），迅速抓住读者眼球。
3. 食材清单：清晰列出所有必需食材，可适当标注份量或替代品提示。
4. 制作步骤：分点撰写，语言必须通俗易懂、具有极强的实操性。每一步的核心动作要加粗或用 emoji 点缀，让新手也能一看就会。
5. 爆款话题（Hashtag）：结尾处必须提供 5-8 个自带高流量的精准话题（例如：#小红书爆款美食 #神仙吃法 #懒人食谱 等）。
6. 排版与字数：全文总字数严格控制在 800 字以内。大量使用 emoji 提升阅读体验，段落之间留出空行，保持排版呼吸感。

食谱信息：
标题：{recipe.title}
食材：{', '.join(recipe.ingredients)}
步骤：
{chr(10).join(recipe.steps)}

请严格返回 JSON 格式，包含以下字段：
- title: 笔记标题 (绝对不能超过18个字)
- content: 笔记正文
"""
    completion = await ai_client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": "你是一个熟练掌握小红书爆款文案风格的美食博主，只返回 JSON。"},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"}
    )
    
    import json
    content = completion.choices[0].message.content
    if content:
        return json.loads(content)
    return {"title": recipe.title, "content": "\n".join(recipe.steps)}

async def download_image(url: str, save_dir: str) -> Optional[str]:
    """下载图片到本地"""
    try:
        # 使用更复杂的头部绕过防盗链
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
            "Referer": "https://food52.com/"
        }
        async with httpx.AsyncClient(follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
            
            # 从 URL 获取后缀，默认 jpg
            ext = url.split('.')[-1][:4] if '.' in url else 'jpg'
            # 过滤特殊字符
            ext = re.sub(r'[^a-zA-Z0-9]', '', ext)
            if ext not in ['jpg', 'jpeg', 'png', 'webp']:
                 ext = 'jpg'
                 
            file_path = os.path.join(save_dir, f"{uuid.uuid4().hex}.{ext}")
            with open(file_path, 'wb') as f:
                f.write(response.content)
            return file_path
    except Exception as e:
        print(f"下载图片失败 {url}: {e}")
        return None

def sign_xhs_request(uri, data=None, a1="", web_session=""):
    """
    小红书请求签名函数 (占位，实际需要更复杂的逆向算法或使用第三方接口服务)
    通常使用 xhs 库时，如果不提供 sign 函数，部分接口可能无法调用。
    由于签名算法经常变动，建议寻找开源的签名服务。
    这里为了演示，提供一个空实现，具体使用时需要查阅 xhs 库文档。
    """
    return {}

async def publish_to_xiaohongshu(title: str, content: str, image_urls: List[str], video_url: Optional[str] = None) -> str:
    """将笔记发布到小红书"""
    # 初始化小红书客户端
    # xhs_client = XhsClient(XHS_COOKIE, sign=sign_xhs_request)
    
    # 临时目录用于存放下载的图片和视频
    # 使用长效目录，以便 playwright 有时间读取文件
    import shutil
    temp_dir = os.path.join(os.getcwd(), "temp_media")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        # 优先发布视频
        if video_url:
            print(f"正在下载视频: {video_url}")
            # 使用 yt-dlp 下载视频
            video_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}.mp4")
            ydl_opts = {
                'outtmpl': video_path,
                'quiet': True,
                'no_warnings': True,
                'nocheckcertificate': True,
                'ignoreerrors': True,
                'logger': type('DummyLogger', (object,), {'debug': lambda s: None, 'warning': lambda s: None, 'error': lambda s: None})(),
            }
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: yt_dlp.YoutubeDL(params=ydl_opts).download([video_url]) # type: ignore
                )
            except Exception as e:
                 raise RuntimeError(f"下载视频失败: {e}")
                 
            if os.path.exists(video_path):
                # 使用 Playwright 发布视频
                result = await publish_with_playwright(title, content, video_path=video_path)
                return result
            else:
                 raise RuntimeError("视频下载后文件不存在")

        # 没有视频则发布图文
        local_image_paths = []
        for url in image_urls[:9]: # 限制最多 9 张图
            local_path = await download_image(url, temp_dir)
            if local_path:
                local_image_paths.append(local_path)
        
        if not local_image_paths:
            # For testing, use a dummy image if download fails
            print("Failed to download images, using a test image...")
            test_img = os.path.join(os.getcwd(), "xiaohongshu-recipe-mcp", "test_lemon.jpg")
            if os.path.exists(test_img):
                 local_image_paths.append(test_img)
            else:
                 raise ValueError("没有成功下载到任何图片或视频，无法发布笔记")

            
        # 使用 Playwright 发布图文
        result = await publish_with_playwright(title, content, image_paths=local_image_paths)
        return result
        
    finally:
        # 延迟清理临时文件，确保 playwright 读取完成
        # 实际上为了排错，暂时不清理
        pass

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools."""
    return [
        types.Tool(
            name="generate_and_publish_recipe",
            description="从给定的食谱网页URL抓取内容，使用AI生成小红书笔记风格的文案，并自动打开浏览器发布到小红书（首次需扫码）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的食谱网页URL"
                    }
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="draft_recipe_note",
            description="仅生成小红书笔记草稿（抓取网页+生成文案+获取图片链接），不进行发布。",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的食谱网页URL"
                    }
                },
                "required": ["url"]
            }
        )
    ]

def run_background_publish(url: str):
    """在一个独立的进程中运行发布任务，避免阻塞 MCP"""
    script = f"""
import asyncio
import sys
import io
from server import extract_recipe_from_url, generate_xiaohongshu_post, publish_to_xiaohongshu
from dotenv import load_dotenv

load_dotenv()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

async def main():
    try:
        # 获取命令行参数传入的 URL，如果在测试环境下没有参数，则使用默认的
        url = sys.argv[1] if len(sys.argv) > 1 else 'https://www.thekitchn.com/grandmas-famous-lemon-bars-recipe-review-23770816'
        print(f"\\n正在准备处理网址: {url}\\n")
        recipe_data = await extract_recipe_from_url(url)
        print("网页抓取完毕，开始生成文案...")
        post_data = await generate_xiaohongshu_post(recipe_data)
        print("文案生成完毕，准备调用浏览器发布...")
        await publish_to_xiaohongshu(
            title=post_data['title'],
            content=post_data['content'],
            image_urls=recipe_data.image_urls,
            video_url=recipe_data.video_url
        )
        print("\\n================================")
        print("✅ 全部流程执行完毕，发布成功！")
        print("================================")
    except Exception as _e:
        print("\\n================================")
        print("❌ 执行失败: " + str(_e))
        print("================================")
        import traceback
        traceback.print_exc()
        with open('publish_error.log', 'w') as f:
            f.write(str(_e))
    finally:
        print("\\n控制台将在 15 秒后自动关闭...")
        await asyncio.sleep(15)

if __name__ == '__main__':
    asyncio.run(main())
"""
    # 写入临时脚本并执行
    with tempfile.NamedTemporaryFile('w', delete=False, suffix='.py', encoding='utf-8') as f:
        f.write(script)
        temp_script_path = f.name
        
    # 在后台启动进程，通过命令行传参 URL
    if os.name == 'nt': # Windows
        subprocess.Popen([sys.executable, temp_script_path, url], creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen([sys.executable, temp_script_path, url], start_new_session=True)

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Handle tool execution requests."""
    if not arguments:
        raise ValueError("Missing arguments")

    if name == "generate_and_publish_recipe":
        url = arguments.get("url")
        if not url:
            raise ValueError("Missing url parameter")
            
        try:
            # 改为异步触发，立刻返回给客户端
            run_background_publish(url)
            
            return [types.TextContent(
                type="text",
                text=f"✅ 发布任务已在后台启动！\n\n请注意你的桌面，稍后会自动弹出一个浏览器窗口。\n如果是首次运行，请在弹出的浏览器中用手机扫码登录小红书。"
            )]
        except Exception as e:
            return [types.TextContent(type="text", text=f"后台任务启动失败: {str(e)}")]
            
    elif name == "draft_recipe_note":
         url = arguments.get("url")
         if not url:
             raise ValueError("Missing url parameter")
             
         try:
            # 1. 抓取网页并提取结构化数据
            recipe_data = await extract_recipe_from_url(url)
            
            # 2. 生成文案
            post_data = await generate_xiaohongshu_post(recipe_data)
            
            result = f"""
## 生成的笔记草稿

### 标题
{post_data['title']}

### 正文
{post_data['content']}

### 提取的视频链接
{recipe_data.video_url if recipe_data.video_url else '未找到视频'}

### 提取的图片链接 (前9张)
{chr(10).join(recipe_data.image_urls[:9])}
"""
            return [types.TextContent(type="text", text=result)]
         except Exception as e:
            return [types.TextContent(type="text", text=f"执行草稿生成失败: {str(e)}")]

    raise ValueError(f"Unknown tool: {name}")

async def main():
    # Run the server using stdin/stdout streams
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="xiaohongshu-recipe",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())