import requests
import json
import csv
import re
import time
import os
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, InvalidSessionIdException
from bs4 import BeautifulSoup

# ========== 配置区域 ==========
FEED_CURL_FILE = "curl_feed.txt"          # Feed cURL 文件
OUTPUT_FILE = f"toutiao_articles_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
ENABLE_PAGINATION = False                  # 是否翻页（通常关闭）
MAX_PAGES = 3                              # 最大翻页数

# Chrome Driver 本地路径（可选；不存在时将自动下载）
CHROME_DRIVER_PATH = r""
HEADLESS = True                            # 无头模式（不显示浏览器窗口）
# ================================

# 粉丝数和获赞数缓存（避免重复请求同一作者）
# 缓存格式：uid -> (粉丝数, 获赞数, 作者名, 主页URL)
profile_cache = {}

def read_curl_from_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read().strip()


def resolve_chrome_driver_path() -> str | None:
    """
    优先使用本地 CHROME_DRIVER_PATH；不存在时尝试 webdriver-manager 自动下载。
    返回 None 表示交由 Selenium Manager/默认逻辑处理。
    """
    if CHROME_DRIVER_PATH and os.path.exists(CHROME_DRIVER_PATH):
        return CHROME_DRIVER_PATH

    # webdriver-manager 在 requirements.txt 里已加入，这里做兜底。
    try:
        from webdriver_manager.chrome import ChromeDriverManager

        return ChromeDriverManager().install()
    except ModuleNotFoundError:
        # 当前环境可能未安装 webdriver-manager；此时直接返回 None，让 Selenium Manager/默认逻辑处理。
        return None
    except Exception as e:
        print(f"[浏览器] 本地 Chrome driver 不存在，且自动安装失败：{e}")
        return None


def parse_curl(curl_cmd):
    """解析 cURL 命令，返回 (base_url, headers, cookies, params)"""
    url_match = re.search(r"curl\s+'([^']+)'|curl\s+\"([^\"]+)\"", curl_cmd)
    if not url_match:
        raise ValueError("无法从 cURL 中提取 URL")
    url = url_match.group(1) or url_match.group(2)

    headers = {}
    header_matches = re.findall(r"-H\s+'([^']+)'|-H\s+\"([^\"]+)\"", curl_cmd)
    for match in header_matches:
        header_str = match[0] or match[1]
        if ': ' in header_str:
            key, value = header_str.split(': ', 1)
            headers[key.lower()] = value

    cookies = {}
    cookie_match = re.search(r"-b\s+'([^']+)'|-b\s+\"([^\"]+)\"", curl_cmd)
    if cookie_match:
        cookie_str = cookie_match.group(1) or cookie_match.group(2)
        for item in cookie_str.split('; '):
            if '=' in item:
                k, v = item.split('=', 1)
                cookies[k] = v

    parsed_url = urlparse(url)
    params = parse_qs(parsed_url.query)
    params = {k: v[0] for k, v in params.items()}
    return url.split('?')[0], headers, cookies, params

def fetch_page(session, base_url, headers, cookies, params):
    """请求 Feed 接口，返回 JSON 数据"""
    try:
        resp = session.get(base_url, headers=headers, params=params, cookies=cookies, timeout=15)
        print(f"Feed 状态码: {resp.status_code}")
        if resp.status_code == 200 and resp.text:
            return resp.json()
        else:
            print(f"Feed 响应为空或状态码异常，内容预览: {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"Feed 请求异常: {e}")
        return None

def parse_number(text):
    """将可能包含 '万'、'亿' 的字符串转换为整数"""
    if not text:
        return 0
    try:
        text = str(text).strip()
        if '万' in text:
            return int(float(text.replace('万', '')) * 10000)
        elif '亿' in text:
            return int(float(text.replace('亿', '')) * 100000000)
        else:
            cleaned = re.sub(r'[^\d\.]', '', text)
            return int(float(cleaned)) if cleaned else 0
    except:
        return 0

class AuthorCrawler:
    """专用于获取作者主页粉丝数和获赞数的浏览器爬虫（单次使用，自动关闭）"""
    def __init__(self, headless=True, driver_path=None):
        self.headless = headless
        self.driver_path = driver_path
        self.driver = None
        self._init_browser()

    def _init_browser(self):
        options = ChromeOptions()
        options.page_load_strategy = 'eager'
        if self.headless:
            options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--disable-gpu")
        options.add_argument("--lang=zh-CN")
        # 隐藏自动化特征
        options.add_argument("--incognito")
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        options.add_argument(f'--user-agent={user_agent}')

        driver_path = self.driver_path if self.driver_path and os.path.exists(self.driver_path) else resolve_chrome_driver_path()
        if not driver_path:
            print("[浏览器] Chrome driver 路径未能解析到，将让 Selenium 自行处理。")
            service = ChromeService()
        else:
            service = ChromeService(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=options)

        # 注入反检测脚本
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                if (navigator.hasOwnProperty('webdriver')) { delete navigator.webdriver; }
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
            """
        })
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(20)
        self.driver = driver

    def ensure_alive(self):
        try:
            self.driver.current_url
            return True
        except (WebDriverException, InvalidSessionIdException, AttributeError):
            return False

    def get_author_stats(self, uid):
        """
        访问作者主页，返回 (粉丝数, 获赞数, 作者名, 当前页面的URL)
        如果失败，返回 (0, 0, "未知", None)
        """
        url = f"https://www.toutiao.com/c/user/{uid}/"
        print(f"[浏览器] 正在获取作者 UID={uid} 的信息...")
        if not self.ensure_alive():
            print("[浏览器] 驱动失效，无法获取")
            return 0, 0, "未知", None

        try:
            self.driver.get(url)
            # 等待页面主体出现
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(1.5)  # 等待动态数据填充

            html = self.driver.page_source
            soup = BeautifulSoup(html, 'html.parser')

            # ---------- 提取作者名 ----------
            author_name = "未知"
            name_elem = soup.find('span', class_='name')
            if not name_elem:
                name_elem = soup.find('div', class_='name')
            if name_elem:
                author_name = name_elem.get_text(strip=True)
            else:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text()
                    title = re.sub(r'\s*[–—\-|]\s*(今日头条|头条|今日)$', '', title, flags=re.IGNORECASE).strip()
                    title = re.sub(r'的头条主页$', '', title).strip()
                    if title:
                        author_name = title

            # ---------- 提取粉丝数和获赞数 ----------
            fans_count = 0
            like_count = 0
            stat_items = soup.find_all('button', class_='stat-item')
            if stat_items:
                for btn in stat_items:
                    btn_text = btn.get_text(strip=True)
                    aria = btn.get('aria-label', '')
                    num_span = btn.find('span', class_='num')
                    if not num_span:
                        continue
                    num_value = parse_number(num_span.get_text(strip=True))
                    if '粉丝' in btn_text or '粉丝' in aria:
                        fans_count = num_value
                    elif '获赞' in btn_text or '赞' in btn_text or '点赞' in btn_text or '获赞' in aria:
                        like_count = num_value
                # 如果仍识别不出，按顺序假设第一个为获赞，第二个为粉丝
                if fans_count == 0 and like_count == 0 and len(stat_items) >= 2:
                    num_span0 = stat_items[0].find('span', class_='num')
                    num_span1 = stat_items[1].find('span', class_='num')
                    if num_span0:
                        like_count = parse_number(num_span0.get_text(strip=True))
                    if num_span1:
                        fans_count = parse_number(num_span1.get_text(strip=True))
            else:
                # 降级查找
                fan_elem = soup.find(lambda tag: tag.name and '粉丝' in tag.get_text(strip=True))
                if fan_elem:
                    text = fan_elem.get_text(strip=True)
                    match = re.search(r'([\d,.]+(?:万|亿)?)', text)
                    if match:
                        fans_count = parse_number(match.group(1))
                like_elem = soup.find(lambda tag: tag.name and '获赞' in tag.get_text(strip=True))
                if like_elem:
                    text = like_elem.get_text(strip=True)
                    match = re.search(r'([\d,.]+(?:万|亿)?)', text)
                    if match:
                        like_count = parse_number(match.group(1))
                else:
                    aria_elem = soup.find(attrs={'aria-label': re.compile(r'获赞')})
                    if aria_elem:
                        aria = aria_elem.get('aria-label', '')
                        match = re.search(r'([\d,.]+(?:万|亿)?)', aria)
                        if match:
                            like_count = parse_number(match.group(1))

            print(f"[浏览器] UID={uid} 粉丝={fans_count}, 获赞={like_count}, 名称={author_name}")
            current_url = self.driver.current_url
            return fans_count, like_count, author_name, current_url

        except Exception as e:
            print(f"[浏览器] 获取作者信息失败: {e}")
            return 0, 0, "未知", None

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
            self.driver = None

def get_author_stats(uid):
    """对外接口：获取作者粉丝数和获赞数（带缓存）"""
    if uid in profile_cache:
        return profile_cache[uid]  # 返回 (fans, digg, name, url)

    crawler = None
    try:
        crawler = AuthorCrawler(headless=HEADLESS, driver_path=CHROME_DRIVER_PATH)
        fans, digg, name, url = crawler.get_author_stats(uid)
        profile_cache[uid] = (fans, digg, name, url)
        return fans, digg, name, url
    except Exception as e:
        # 驱动初始化失败时，也要保证 Feed 解析能继续进行（作者统计置 0）。
        print(f"[浏览器] 获取作者信息失败（回退为 0）：{e}")
        profile_cache[uid] = (0, 0, "未知", None)
        return 0, 0, "未知", None
    finally:
        try:
            if crawler:
                crawler.close()
        except Exception:
            pass

def timestamp_to_str(ts):
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else ''

def extract_category(mm_str):
    if not mm_str:
        return ''
    try:
        data = json.loads(mm_str)
        return max(data, key=data.get) if data else ''
    except:
        return ''

def parse_article(item):
    uid = None
    source_open_url = item.get('source_open_url', '')
    if source_open_url:
        uid_match = re.search(r'uid=(\d+)', source_open_url)
        if uid_match:
            uid = uid_match.group(1)
    if not uid:
        media_info = item.get('media_info', {})
        user_id = media_info.get('user_id', '')
        if user_id and user_id.isdigit():
            uid = user_id

    fans, digg, author_name, _ = get_author_stats(uid) if uid else (0, 0, '未知', '')

    author = item.get('media_name') or author_name

    # 构造作者主页链接（优先使用 source_open_url，否则用 uid 拼接）
    if source_open_url and source_open_url.startswith('http'):
        author_homepage = source_open_url
    elif uid:
        author_homepage = f"https://www.toutiao.com/c/user/{uid}/"
    else:
        author_homepage = ''

    return {
        '文章ID': item.get('group_id'),
        '标题': item.get('title'),
        '摘要': item.get('Abstract'),
        '发布时间': timestamp_to_str(item.get('publish_time')),
        '推荐时间': timestamp_to_str(item.get('behot_time')),
        '阅读数': item.get('read_count', 0),
        '点赞数': item.get('digg_count', 0),
        '评论数': item.get('comment_count', 0),
        '分享数': item.get('share_count', 0),
        '收藏数': item.get('repin_count', 0),
        '作者': author,
        '粉丝数': fans,
        '获赞数': digg,
        '是否认证': item.get('user_verified', 0),
        '认证信息': item.get('verified_content', ''),
        '文章链接': item.get('article_url'),
        '作者主页': author_homepage,   # 新增
        '图片数量': item.get('gallary_image_count', 0),
        '是否有图': item.get('has_image', False),
        '分类': extract_category(item.get('optional_data', {}).get('mm_category_three', ''))
    }

def is_article(item):
    # 兼容字段缺失/数值化：有的接口不返回 has_video，而有的返回 0/1。
    has_video = item.get('has_video', False)
    return (not bool(has_video)) and item.get('cell_type') != 48

def main():
    print("从文件中读取 Feed cURL 命令...")
    feed_curl = read_curl_from_file(FEED_CURL_FILE)
    try:
        feed_base_url, feed_headers, feed_cookies, feed_params = parse_curl(feed_curl)
    except Exception as e:
        print(f"解析 Feed cURL 失败: {e}")
        return

    headers = feed_headers
    cookies = feed_cookies
    print(f"Feed 请求地址: {feed_base_url}")
    print(f"Feed 参数个数: {len(feed_params)}")

    session = requests.Session()
    all_articles = []
    page_num = 1

    while True:
        print(f"\n正在获取第 {page_num} 页 Feed...")
        data = fetch_page(session, feed_base_url, headers, cookies, feed_params)
        if not data or 'data' not in data:
            print("获取 Feed 数据失败，停止。")
            break

        items = data.get('data', [])
        if not items:
            print("本页无数据，停止。")
            break

        for item in items:
            if is_article(item):
                try:
                    article = parse_article(item)
                    all_articles.append(article)
                except Exception as e:
                    print(f"解析文章时出错: {e}")

        print(f"本页共 {len(items)} 条，其中图文文章 {sum(1 for i in items if is_article(i))} 篇")

        if not ENABLE_PAGINATION:
            break

        next_info = data.get('next', {})
        new_max_behot = next_info.get('max_behot_time')
        if new_max_behot and str(new_max_behot) != feed_params.get('max_behot_time'):
            feed_params['max_behot_time'] = str(new_max_behot)
            print(f"更新 max_behot_time 为 {new_max_behot}，但 msToken 和 a_bogus 未更新，后续可能失败")
        else:
            break

        page_num += 1
        if page_num > MAX_PAGES:
            break
        time.sleep(3)

    if all_articles:
        fieldnames = [
            '文章ID', '标题', '摘要', '发布时间', '推荐时间',
            '阅读数', '点赞数', '评论数', '分享数', '收藏数',
            '作者', '粉丝数', '获赞数', '是否认证', '认证信息', '文章链接',
            '作者主页',  # 新增
            '图片数量', '是否有图', '分类'
        ]
        with open(OUTPUT_FILE, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_articles)
        print(f"\n完成！共获取 {len(all_articles)} 篇文章，已保存到 {OUTPUT_FILE}")
    else:
        print("未获取到任何文章")

if __name__ == '__main__':
    main()