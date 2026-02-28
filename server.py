import asyncio
import os
import re
import tempfile
import uuid
import sys
import subprocess
import threading
import io

# ✅ Fix: 强制 stdout/stderr 使用 UTF-8 并开启行缓冲，防止 Windows 下 emoji 崩溃以及输出空白
try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    elif hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)
    elif hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
except Exception:
    pass
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
    """从任意网页或本地HTML提取食谱内容和图片"""
    html_content = ""
    # 判断是否为本地文件
    if os.path.isfile(url):
        try:
            with open(url, 'r', encoding='utf-8') as f:
                html_content = f.read()
        except Exception as e:
            print(f"读取本地文件失败 ({e})")
            return RecipeData(title="", ingredients=[], steps=[], image_urls=[], video_url=None)
    else:
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
    content_selectors = [
        '.card-recipe-detail',
        '.recipe-detail',
        'article',
        'main',
        '.recipe-content',
        '.post-content',
        '.entry-content',
        '#recipe-block',
        '[class*="recipe-content"]',
        '[class*="recipe-detail"]',
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
            
        # 搜索 iframe 中的 youtube/vimeo 链接
        if not video_url:
            for iframe in main_content.find_all('iframe'):
                src = iframe.get('src')
                if src and ('youtube.com/embed/' in src or 'player.vimeo.com/video/' in src):
                    video_url = src
                    # 把 URL 格式化为标准链接方便 yt-dlp 解析
                    if 'youtube.com/embed/' in src:
                        video_id = src.split('youtube.com/embed/')[1]
                        if '?' in video_id:
                            video_id = video_id.split('?')[0]
                        video_url = f"https://www.youtube.com/watch?v={video_id}"
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
    exclude_classes = ['sidebar', 'widget', 'related', 'recommended', 'footer', 'nav', 'author', 'promo', 'category', 'categories', 'recipe-card', 'index-categories']
    
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
                
                # 过滤外链图或跳转到其他食谱的卡片大图
                if parent.name == 'a':
                    href = parent.get('href', '')
                    # 如果跳转的不是当前网页本身，也不是大图片，那么大概率是其他食谱列表项或者广告
                    if href and url not in href and not href.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')) and not href.startswith('#'):
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
            if src.startswith('http') and not any(skip_word in src.lower() for skip_word in ['icon', 'logo', 'avatar', 'gif', 'svg', 'thumb', 'small', '150x150', '300x300', 'impression', 'pixel', 'dummy']):
                # 如果 URL 中有查询参数控制大小（比如 wp 的图像），尽量保留原图
                import re
                src = re.sub(r'-\d+x\d+\.(jpg|jpeg|png)$', r'.\1', src, flags=re.IGNORECASE)
                if src not in images:
                    images.append(src)
            elif src.startswith('file://') or os.path.isabs(src): # 支持本地图片
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
        # ✅ Fix: lambda 需要接受 (self, msg) 两个参数，否则 yt-dlp 调用时报 TypeError
        'logger': type('DummyLogger', (object,), {'debug': lambda self, msg: None, 'warning': lambda self, msg: None, 'error': lambda self, msg: None})(),
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
7. 格式警告：小红书正文不支持 Markdown 格式！请绝对不要使用 `**加粗**`、`# 标题` 或 `- 列表` 等 Markdown 语法，请仅使用纯文本、换行和 Emoji 进行排版。

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

async def download_image(url: str, save_dir: str, referer: str = "") -> Optional[str]:
    """下载图片到本地，支持动态 Referer 以绕过不同网站的防盗链，同时也支持本地图片路径"""
    try:
        if url.startswith('file://'):
            import shutil
            local_path = url[7:]
            if os.name == 'nt' and local_path.startswith('/'): # windows 下 file:///C:/ 变成 /C:/
                local_path = local_path[1:]
            if os.path.exists(local_path):
                ext = local_path.split('.')[-1][:4] if '.' in local_path else 'jpg'
                file_path = os.path.join(save_dir, f"{uuid.uuid4().hex}.{ext}")
                shutil.copy2(local_path, file_path)
                return file_path
            return None
        elif os.path.isabs(url) and os.path.exists(url):
            import shutil
            ext = url.split('.')[-1][:4] if '.' in url else 'jpg'
            file_path = os.path.join(save_dir, f"{uuid.uuid4().hex}.{ext}")
            shutil.copy2(url, file_path)
            return file_path

        from urllib.parse import urlparse
        # 动态生成 Referer：使用来源页面的域名，若未指定则从图片 URL 推断
        if not referer:
            parsed = urlparse(url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"

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
            "Referer": referer
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



async def publish_to_xiaohongshu(title: str, content: str, image_urls: List[str], source_url: str = "", video_url: Optional[str] = None, save_draft: bool = False) -> str:
    """将笔记发布到小红书"""
    from urllib.parse import urlparse

    # 从原始页面 URL 提取域名，用于图片下载时的 Referer（绕防盗链）
    image_referer = ""
    if source_url:
        parsed = urlparse(source_url)
        image_referer = f"{parsed.scheme}://{parsed.netloc}/"

    # 临时目录用于存放下载的图片和视频，使用长效目录以便 playwright 有时间读取文件
    temp_dir = os.path.join(os.getcwd(), "temp_media")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        # 优先发布视频
        if video_url:
            print(f"正在准备下载视频: {video_url}")
            video_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}.mp4")
            
            cookie_path = os.path.join(os.getcwd(), 'cookies.txt')
            is_youtube = 'youtube.com' in video_url or 'youtu.be' in video_url
            
            # 针对 YouTube 的交互式 Cookie 提示
            if is_youtube:
                while True:
                    if os.path.exists(cookie_path):
                        print(f"✅ 找到 cookies.txt，开始尝试下载...")
                        break
                    else:
                        print("\n" + "!"*50)
                        print("⚠️ 检测到 YouTube 视频，且当前目录缺少 cookies.txt 文件。")
                        print("👉 请在浏览器中安装 Get cookies.txt 扩展，导出并保存到本项目根目录的 cookies.txt 文件中。")
                        print("!"*50)
                        input("保存完成后，请按【回车键】继续...")
            
            # 尝试下载
            download_success = False
            while not download_success:
                ydl_opts = {
                    'outtmpl': video_path,
                    'quiet': False, # 关闭 quiet 以便用户能看到 bot 检测错误
                    'no_warnings': False,
                    'nocheckcertificate': True,
                    'ignoreerrors': False, # 改为 False 让外部捕获
                }
                
                if is_youtube:
                    # 针对 YouTube 添加 cookie 和 js_engine
                    if os.path.exists(cookie_path):
                        ydl_opts['cookiefile'] = cookie_path
                    ydl_opts['js_engine'] = 'nodejs' # 使用用户提到的 nodejs 绕过

                try:
                    import subprocess
                    if is_youtube and os.path.exists(cookie_path):
                        # 如果是 YouTube，由于 Python API 内部直接调用有时无法正确挂载 node 环境来解密 JS 挑战
                        # 这里直接采用 subprocess 调用命令行的 yt-dlp 来实现与用户终端一致的行为
                        print("检测到 YouTube 链接，正在通过 subprocess 唤起 yt-dlp...")
                        cmd = [
                            'yt-dlp', 
                            '--cookies', cookie_path, 
                            '--js-runtimes', 'node', 
                            '--no-check-certificate',
                            '-o', video_path, 
                            video_url
                        ]
                        
                        loop = asyncio.get_running_loop()
                        def run_cmd():
                            # 使用 errors='replace' 来避免 Windows 平台下的解码报错
                            process = subprocess.Popen(
                                cmd, 
                                stdout=subprocess.PIPE, 
                                stderr=subprocess.STDOUT, 
                                text=True, 
                                encoding='utf-8', 
                                errors='replace'
                            )
                            for line in process.stdout: # type: ignore
                                # 将 yt-dlp 的下载进度实时打印出来
                                if '[download]' in line or '[youtube]' in line:
                                    # 为了不刷屏，只打印部分进度
                                    if 'ETA' in line:
                                        print(f"\\r{line.strip()}", end='', flush=True)
                                    else:
                                        print(f"\\n{line.strip()}", flush=True)
                            process.wait()
                            print("\\n")
                            # yt-dlp 返回非 0 即为失败，我们将具体的输出保存起来用于外部捕获关键字
                            if process.returncode != 0:
                                return "error_bot_or_signin" # 用一个固定字符串替代数字，让外面更容易识别出这是可能由于 bot 引起的
                            return process.returncode
                            
                        returncode = await loop.run_in_executor(None, run_cmd)
                        
                        if returncode == "error_bot_or_signin":
                            raise RuntimeError("yt-dlp 执行失败，检测到 bot 或 sign in 相关错误")
                        elif returncode != 0:
                            raise RuntimeError(f"yt-dlp 子进程返回错误码: {returncode}")
                    else:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None,
                            lambda: yt_dlp.YoutubeDL(params=ydl_opts).download([video_url]) # type: ignore
                        )
                    
                    if os.path.exists(video_path):
                        download_success = True
                    else:
                        raise RuntimeError("yt-dlp 执行完成但未生成视频文件")
                        
                except Exception as e:
                    error_msg = str(e).lower()
                    print(f"\n❌ 下载失败: {e}")
                    if is_youtube and ('bot' in error_msg or 'sign in' in error_msg):
                        print("\n" + "!"*50)
                        print("⚠️ 下载失败，可能 cookies.txt 已失效或格式不正确。")
                        print("👉 请重新导出最新的 cookies.txt 文件覆盖原文件。")
                        print("如果想放弃下载该视频转而发布纯图文，请直接关闭本窗口，或者输入 'skip' 并回车。")
                        print("!"*50)
                        user_input = input("更新 cookies.txt 后按【回车键】重试，或输入 skip 放弃视频：")
                        if user_input.strip().lower() == 'skip':
                            print("⏭️ 用户选择放弃视频，降级为图文模式。")
                            break # 跳出 while 循环
                    else:
                        # 其它错误直接跳出让下方代码报错或降级
                        break

            if download_success:
                # 同时并发下载最多 3 张图片作为视频封面图
                cover_image_paths: List[str] = []
                if image_urls:
                    print(f"视频模式：并发下载最多 3 张封面图...")
                    cover_tasks = [
                        download_image(url, temp_dir, referer=image_referer)
                        for url in image_urls[:3]
                    ]
                    cover_results = await asyncio.gather(*cover_tasks, return_exceptions=True)
                    cover_image_paths = [
                        r for r in cover_results
                        if isinstance(r, str) and r
                    ]
                    print(f"封面图下载完成，成功 {len(cover_image_paths)} / {min(len(image_urls), 3)} 张")

                result = await publish_with_playwright(
                    title, content,
                    video_path=video_path,
                    cover_image_paths=cover_image_paths,
                    save_draft=save_draft
                )
                return result
            else:
                print("⚠️ 视频下载失败，已自动降级为图文模式发布...")

        # 没有视频则并发下载图片（asyncio.gather 并发，提升速度）
        print(f"开始并发下载 {min(len(image_urls), 9)} 张图片...")
        download_tasks = [
            download_image(url, temp_dir, referer=image_referer)
            for url in image_urls[:9]  # 限制最多 9 张图
        ]
        results = await asyncio.gather(*download_tasks, return_exceptions=True)
        local_image_paths = [
            r for r in results
            if isinstance(r, str) and r  # 过滤失败的任务（None 或 Exception）
        ]
        print(f"图片下载完成，成功 {len(local_image_paths)} / {min(len(image_urls), 9)} 张")
        
        if not local_image_paths:
            # 兜底：尝试使用当前目录下的测试图片
            print("所有图片下载失败，尝试使用本地测试图片...")
            test_img = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_lemon.jpg")
            if os.path.exists(test_img):
                local_image_paths.append(test_img)
            else:
                raise ValueError("没有成功下载到任何图片或视频，无法发布笔记")
            
        # 使用 Playwright 发布图文
        result = await publish_with_playwright(title, content, image_paths=local_image_paths, save_draft=save_draft)
        return result
        
    finally:
        # 延迟清理临时文件，确保 playwright 读取完成
        # 实际上为了排查问题，暂时不清理
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
            name="generate_and_save_draft_recipe",
            description="抓取食谱网页并生成小红书笔记文案，之后打开浏览器填充内容并点击'暂存离开'，存入草稿箱不立即发布。",
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

def run_background_publish(url: str, save_draft: bool = False):
    """在一个独立的进程中运行发布任务，避免阻塞 MCP"""
    project_root = os.path.dirname(os.path.abspath(__file__))
    script = f"""
import asyncio
import sys
import os
import io

# 将项目根目录添加到路径，确保能导入 server 模块
sys.path.append(r"{project_root}")

from server import extract_recipe_from_url, generate_xiaohongshu_post, publish_to_xiaohongshu
from dotenv import load_dotenv

# 解决 Windows 下 Emoji 打印导致的编码问题，并设置为行缓冲以防输出卡住
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
elif hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

load_dotenv()

async def main():
    try:
        # 获取命令行参数传入的 URL
        target_url = sys.argv[1] if len(sys.argv) > 1 else r"{url}"
        is_draft = sys.argv[2] == "True" if len(sys.argv) > 2 else {"True" if save_draft else "False"}
        print("\\n" + "="*40, flush=True)
        print(f"🚀 捕获到新任务！", flush=True)
        print(f"📍 目标网址: {{target_url}}", flush=True)
        print("="*40 + "\\n", flush=True)
        
        print("🔍 正在抓取并分析网页内容...", flush=True)
        recipe_data = await extract_recipe_from_url(target_url)
        
        print("📝 正在使用 AI 生成爆款推文...", flush=True)
        post_data = await generate_xiaohongshu_post(recipe_data)
        
        print(f"✨ 文案生成成功！标题: {{post_data.get('title', '无标题')}}", flush=True)
        
        if recipe_data.video_url:
            print(f"📹 发现视频，准备下载并发布...", flush=True)
        elif recipe_data.image_urls:
            print(f"🖼️ 发现 {{len(recipe_data.image_urls)}} 张图片，准备下载并发布...", flush=True)
            
        print("🌐 正在启动浏览器准备发布...", flush=True)
        
        await publish_to_xiaohongshu(
            title=post_data['title'],
            content=post_data['content'],
            image_urls=recipe_data.image_urls,
            source_url=target_url,
            video_url=recipe_data.video_url,
            save_draft=is_draft
        )
        print("\\n================================", flush=True)
        print("✅ 全部流程执行完毕，已成功发布！", flush=True)
        print("================================", flush=True)
    except Exception as _e:
        print("\\n" + "!"*40, flush=True)
        print("❌ 后台执行发生错误件", flush=True)
        print(str(_e), flush=True)
        print("!"*40 + "\\n", flush=True)
        import traceback
        traceback.print_exc()
        # 记录错误到本地文件便于排查
        error_log_path = os.path.join(r"{project_root}", 'publish_error.log')
        with open(error_log_path, 'w', encoding='utf-8') as f:
            f.write(str(_e))
            f.write("\\n\\n")
            traceback.print_report(file=f)
    finally:
        print("\\n⏳ 本控制台将在 30 秒后自动关闭...", flush=True)
        await asyncio.sleep(30)

if __name__ == '__main__':
    asyncio.run(main())
"""
    # 写入临时脚本并执行
    import tempfile
    with tempfile.NamedTemporaryFile('w', delete=False, suffix='.py', encoding='utf-8') as f:
        f.write(script)
        temp_script_path = f.name
        
    # 在后台启动进程，通过命令行传参 URL，并指定工作目录
    import subprocess
    env = os.environ.copy()
    env["PYTHONPATH"] = project_root
    env["PYTHONUNBUFFERED"] = "1"  # 强制彻底关闭 Python 的输出缓冲
    
    if os.name == 'nt': # Windows
        # 针对 Windows 路径带空格的情况，手动构造命令字符串并作为单一字符串传入
        # 避免 subprocess.Popen 列表传参时自动转义双引号
        command = f'cmd /k "chcp 65001 >nul & "{sys.executable}" "{temp_script_path}" "{url}" "{save_draft}""'
        subprocess.Popen(
            command, 
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=project_root,
            env=env
        )
    else:
        subprocess.Popen(
            [sys.executable, temp_script_path, url, str(save_draft)], 
            start_new_session=True,
            cwd=project_root,
            env=env
        )

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Handle tool execution requests."""
    if not arguments:
        raise ValueError("Missing arguments")

    if name in ["generate_and_publish_recipe", "generate_and_save_draft_recipe"]:
        url = arguments.get("url")
        if not url:
            raise ValueError("Missing url parameter")
            
        is_draft = (name == "generate_and_save_draft_recipe")
        try:
            # 改为异步触发，立刻返回给客户端
            run_background_publish(url, save_draft=is_draft)
            
            action_text = "存草稿（暂存离开）" if is_draft else "发布"
            return [types.TextContent(
                type="text",
                text=f"✅ {action_text}任务已在后台启动！\n\n请注意你的桌面，稍后会自动弹出一个浏览器窗口。\n如果是首次运行，请在弹出的浏览器中用手机扫码登录小红书。"
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