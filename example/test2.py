# t4_toutiao_crawler_mixed.py - 5列表浏览器 + 2详情浏览器混合版
# 特点：
# - 5个浏览器并行获取文章列表（可配置）
# - 2个浏览器并行解析文章详情（粉丝数、阅读数、点赞、评论、正文）
# - 黑名单自动持久化，失败3次后过滤
# - 图片下载顺序存储
# - 自动导出Excel和文本文件

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import time
import random
import re
import json
import pandas as pd
from datetime import datetime, timedelta
import os
import webbrowser
import threading
import logging
import queue
import concurrent.futures
from urllib.parse import urljoin
import requests
from requests.adapters import HTTPAdapter

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== 全局共享数据（线程安全）====================
class SharedData:
    def __init__(self):
        self.seen_articles = set()
        self.seen_lock = threading.Lock()
        self.blacklist_authors = set()
        self.blacklist_lock = threading.Lock()
        self.author_fail_count = {}
        self.fail_lock = threading.Lock()
        self.cache = {}                     # 缓存粉丝数、阅读数等
        self.cache_lock = threading.Lock()
        self.total_processed = 0
        self.processed_lock = threading.Lock()
        self.cache_file = "shared_cache.json"
        self.blacklist_file = "blacklist.json"
        self.load_cache()
        self.load_blacklist()

    def load_cache(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.cache = data.get('cache', {})
                    logger.info(f"加载缓存: {len(self.cache)} 条")
        except Exception as e:
            logger.error(f"加载缓存失败: {e}")

    def save_cache(self):
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump({'cache': self.cache}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存缓存失败: {e}")

    def load_blacklist(self):
        try:
            if os.path.exists(self.blacklist_file):
                with open(self.blacklist_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.blacklist_authors = set(data.get('blacklist', []))
                    self.author_fail_count = data.get('fail_count', {})
                logger.info(f"加载黑名单: {len(self.blacklist_authors)} 个作者")
        except Exception as e:
            logger.error(f"加载黑名单失败: {e}")

    def save_blacklist(self):
        try:
            data = {
                'blacklist': list(self.blacklist_authors),
                'fail_count': self.author_fail_count
            }
            with open(self.blacklist_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存黑名单失败: {e}")

    def add_seen(self, article_id):
        with self.seen_lock:
            if article_id in self.seen_articles:
                return False
            self.seen_articles.add(article_id)
            return True

    def is_blacklisted(self, author_url):
        with self.blacklist_lock:
            return author_url in self.blacklist_authors

    def add_fail(self, author_url):
        with self.fail_lock:
            count = self.author_fail_count.get(author_url, 0) + 1
            self.author_fail_count[author_url] = count
            if count >= 3:
                with self.blacklist_lock:
                    self.blacklist_authors.add(author_url)
                self.save_blacklist()
                return True
            return False

    def get_cache(self, key):
        with self.cache_lock:
            return self.cache.get(key)

    def set_cache(self, key, value):
        with self.cache_lock:
            self.cache[key] = value
        self.save_cache()  # 简单每次保存

    def inc_processed(self):
        with self.processed_lock:
            self.total_processed += 1
            return self.total_processed


shared = SharedData()

# ==================== 列表获取浏览器类（仅滚动获取文章链接）====================
class ListCrawler:
    def __init__(self, headless=False):
        self.headless = headless
        self.driver = self.init_browser()
        self.stats = {'scroll_count': 0}

    def init_browser(self):
        options = webdriver.ChromeOptions()
        options.page_load_strategy = 'eager'
        if self.headless:
            options.add_argument("--headless")
        else:
            options.add_argument("--start-maximized")

        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-web-security")
        options.add_argument("--allow-running-insecure-content")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--lang=zh-CN")
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-hang-monitor")
        options.add_argument("--disable-ipc-flooding-protection")

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
            """
        })
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(20)
        return driver

    def is_valid_author_url(self, url):
        if not url:
            return False
        return url.startswith('https://www.toutiao.com/c/user/') and '/token/' in url

    def _find_article_cards(self, soup):
        cards = []
        for a in soup.find_all('a', class_='title', href=True):
            parent = a
            found = False
            for _ in range(5):
                if parent and 'feed-card-article' in parent.get('class', []):
                    cards.append(parent)
                    found = True
                    break
                parent = parent.parent
                if parent is None:
                    break
            if not found:
                cards.append(a)
        return cards

    def _extract_article_info(self, card):
        try:
            if card.name == 'a':
                title_elem = card
            else:
                title_elem = card.find('a', class_='title')
                if not title_elem:
                    return None

            article_url = title_elem.get('href', '')
            if not article_url.startswith('http'):
                article_url = f"https://www.toutiao.com{article_url}"

            article_id_match = re.search(r'/article/(\d+)/', article_url)
            if not article_id_match:
                return None
            article_id = article_id_match.group(1)

            title = title_elem.get_text(strip=True)
            if not title:
                title = title_elem.get('aria-label', '') or f"文章_{article_id}"

            if any(kw in title.lower() for kw in ['视频', 'video', '直播', 'live']):
                return None

            author = "未知作者"
            author_url = ""
            author_elem = card.find('div', class_='feed-card-footer-cmp-author')
            if not author_elem:
                author_elem = card.find('div', class_='author-info')
            if author_elem:
                author_link = author_elem.find('a', href=re.compile(r'/c/user/'))
                if author_link:
                    author_url = author_link.get('href', '')
                    author = author_link.get_text(strip=True) or "未知作者"
            if author_url and not author_url.startswith('http'):
                author_url = f"https://www.toutiao.com{author_url}"
            valid_author = self.is_valid_author_url(author_url)

            # 黑名单过滤
            if author_url and shared.is_blacklisted(author_url):
                return None

            publish_time = "未知时间"
            time_elem = card.find('div', class_='feed-card-footer-time-cmp')
            if not time_elem:
                time_elem = card.find('div', class_='time')
            if time_elem:
                publish_time = time_elem.get_text(strip=True)

            comment_count = 0
            comment_elem = card.find('div', class_='feed-card-footer-comment-cmp')
            if not comment_elem:
                comment_elem = card.find('div', class_='comment')
            if comment_elem:
                comment_link = comment_elem.find('a')
                if comment_link:
                    aria_label = comment_link.get('aria-label', '')
                    if aria_label:
                        match = re.search(r'评论数?(\d+)', aria_label)
                        if not match:
                            match = re.search(r'(\d+)\s*评论', aria_label)
                        if match:
                            comment_count = int(match.group(1))

            return {
                'article_id': article_id,
                'url': article_url,
                'title': title[:150],
                'author': author,
                'author_url': author_url,
                'publish_time': publish_time,
                'comment_count': comment_count,
                'valid_author': valid_author,
                'found_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            logger.error(f"提取文章信息失败: {e}")
            return None

    def _wake_up_page(self):
        try:
            self.driver.execute_script("""
                var ev = new MouseEvent('mousemove', {
                    view: window,
                    bubbles: true,
                    cancelable: true,
                    clientX: Math.random() * window.innerWidth,
                    clientY: Math.random() * window.innerHeight
                });
                document.dispatchEvent(ev);
            """)
            time.sleep(0.5)
            self.driver.execute_script("""
                var ev = new KeyboardEvent('keydown', {
                    key: ' ',
                    code: 'Space',
                    which: 32,
                    keyCode: 32,
                    bubbles: true
                });
                document.dispatchEvent(ev);
            """)
            time.sleep(0.5)
            self.driver.execute_script("window.scrollBy(0, -200);")
            time.sleep(0.3)
            self.driver.execute_script("window.scrollBy(0, 200);")
            time.sleep(0.3)
            self.driver.execute_script("document.body.click();")
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"唤醒页面时出错: {e}")

    def smart_scroll_for_articles(self, target_count, max_scrolls=300,
                                  max_hours=None, min_comments=None):
        new_articles = []
        effective_scrolls = 0
        physical_scrolls = 0
        last_count = 0
        EXTRA_WAIT_ON_INVALID = 2
        consecutive_invalid = 0

        soup = BeautifulSoup(self.driver.page_source, 'html.parser')
        last_count = len(self._find_article_cards(soup))

        while len(new_articles) < target_count and effective_scrolls < max_scrolls:
            try:
                self.driver.execute_script("window.focus();")
                self.driver.execute_script("document.body.click();")
                time.sleep(0.3)

                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.2)
                self.driver.execute_script("window.scrollBy(0, 100);")
                time.sleep(0.3)

                physical_scrolls += 1

                html = self.driver.page_source
                soup = BeautifulSoup(html, 'html.parser')
                article_cards = self._find_article_cards(soup)
                current_count = len(article_cards)

                new_cards = current_count - last_count
                duplicate_in_new = 0
                filtered_out = 0
                before_adding = len(new_articles)

                for card in article_cards[last_count:]:
                    article_info = self._extract_article_info(card)
                    if not article_info:
                        filtered_out += 1
                        continue
                    article_id = article_info['article_id']

                    if not shared.add_seen(article_id):
                        duplicate_in_new += 1
                        continue

                    if max_hours is not None:
                        hours_ago = parse_hours_ago(article_info['publish_time'])
                        if hours_ago is None or hours_ago > max_hours:
                            filtered_out += 1
                            continue
                    if min_comments is not None and article_info['comment_count'] < min_comments:
                        filtered_out += 1
                        continue

                    new_articles.append(article_info)
                    if len(new_articles) >= target_count:
                        break

                net_increase = len(new_articles) - before_adding
                last_count = current_count

                if new_cards > 0:
                    effective_scrolls += 1
                    consecutive_invalid = 0
                    logger.info(
                        f"[{threading.current_thread().name}] 有效滚动 #{effective_scrolls} (物理 {physical_scrolls}): 新卡片 {new_cards}，重复 {duplicate_in_new}，过滤 {filtered_out}，净增 {net_increase}，累计 {len(new_articles)}"
                    )
                else:
                    consecutive_invalid += 1
                    logger.info(
                        f"[{threading.current_thread().name}] 无效滚动 #{consecutive_invalid} (物理 {physical_scrolls}): 无新卡片，等待 {EXTRA_WAIT_ON_INVALID}s"
                    )
                    time.sleep(EXTRA_WAIT_ON_INVALID)

                    if consecutive_invalid >= 10:
                        logger.info(f"[{threading.current_thread().name}] 执行唤醒操作")
                        self._wake_up_page()
                        consecutive_invalid = 0

                if physical_scrolls > max_scrolls * 10:
                    logger.warning(f"[{threading.current_thread().name}] 物理滚动过多，停止")
                    break

                self.stats['scroll_count'] = effective_scrolls

            except Exception as e:
                logger.error(f"[{threading.current_thread().name}] 滚动出错: {e}")
                time.sleep(1)
                continue

        return new_articles

    def get_article_links_from_channel(self, channel_slug, target_count=100,
                                       max_hours=None, min_comments=None):
        if channel_slug == 'recommend' or channel_slug == '':
            url = "https://www.toutiao.com/"
        else:
            url = f"https://www.toutiao.com/ch/{channel_slug}/"

        logger.info(f"[{threading.current_thread().name}] 开始获取: {url}")
        try:
            self.driver.get(url)
            time.sleep(random.uniform(3, 5))
            try:
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except:
                pass

            articles = self.smart_scroll_for_articles(
                target_count,
                max_hours=max_hours,
                min_comments=min_comments
            )
            valid_articles = [a for a in articles if a['valid_author']]
            logger.info(f"[{threading.current_thread().name}] 获取完成，有效作者文章: {len(valid_articles)}")
            return valid_articles
        except Exception as e:
            logger.error(f"[{threading.current_thread().name}] 获取列表失败: {e}")
            return []

    def close(self):
        try:
            if self.driver:
                self.driver.quit()
        except:
            pass


# ==================== 详情浏览器类（Selenium获取粉丝数、阅读数、点赞、评论、正文）====================
class DetailCrawler:
    def __init__(self, headless=False):
        self.headless = headless
        self.driver = self.init_browser()

    def init_browser(self):
        options = webdriver.ChromeOptions()
        options.page_load_strategy = 'eager'
        if self.headless:
            options.add_argument("--headless")
        else:
            options.add_argument("--start-maximized")

        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-web-security")
        options.add_argument("--allow-running-insecure-content")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--lang=zh-CN")
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-hang-monitor")
        options.add_argument("--disable-ipc-flooding-protection")

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
            """
        })
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(20)
        return driver

    def get_author_fans_count(self, author_url):
        """Selenium获取作者粉丝数（带重试和黑名单）"""
        if shared.is_blacklisted(author_url):
            return 0

        cache_key = f"fans_{author_url}"
        cached = shared.get_cache(cache_key)
        if cached is not None:
            return cached

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                self.driver.get(author_url)
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".stat-item"))
                )
                self.driver.execute_script("window.scrollBy(0, 500);")
                time.sleep(1)

                html = self.driver.page_source
                soup = BeautifulSoup(html, 'html.parser')

                fans_count = 0
                found = False
                stat_items = soup.find_all('button', class_='stat-item')

                # 多种定位方法
                for item in stat_items:
                    aria = item.get('aria-label', '')
                    if '粉丝' in aria:
                        num_span = item.find('span', class_='num')
                        if num_span:
                            fans_count = parse_number(num_span.get_text(strip=True))
                            found = True
                            break
                if not found:
                    for item in stat_items:
                        if '粉丝' in item.get_text(strip=True):
                            num_span = item.find('span', class_='num')
                            if num_span:
                                fans_count = parse_number(num_span.get_text(strip=True))
                                found = True
                                break
                if not found and len(stat_items) >= 2:
                    num_span = stat_items[1].find('span', class_='num')
                    if num_span:
                        fans_count = parse_number(num_span.get_text(strip=True))
                        found = True
                if not found and stat_items:
                    num_span = stat_items[0].find('span', class_='num')
                    if num_span:
                        fans_count = parse_number(num_span.get_text(strip=True))
                        found = True
                if not found:
                    all_text = soup.get_text()
                    match = re.search(r'粉丝\s*(\d+(?:\.\d+)?)([万亿]?)', all_text)
                    if match:
                        fans_count = parse_number(match.group(1) + match.group(2))
                        found = True

                if found:
                    shared.set_cache(cache_key, fans_count)
                    return fans_count
                else:
                    logger.warning(f"无法解析粉丝数: {author_url}")
                    return 0
            except Exception as e:
                logger.warning(f"获取粉丝数失败 (尝试 {attempt+1}): {e}")
                if attempt < max_retries:
                    time.sleep(2)
                else:
                    if shared.add_fail(author_url):
                        logger.warning(f"作者 {author_url} 已加入黑名单")
                    return 0
        return 0

    def get_article_read_count_exact(self, article_id, author_url, article_url):
        """Selenium获取阅读数"""
        if shared.is_blacklisted(author_url):
            return 0

        cache_key = f"read_{article_id}_{author_url}"
        cached = shared.get_cache(cache_key)
        if cached is not None:
            return cached

        try:
            self.driver.get(author_url)
            time.sleep(random.uniform(2, 3))
            try:
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except:
                pass

            for _ in range(5):
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(random.uniform(1, 2))
                if article_id in self.driver.page_source:
                    break

            html = self.driver.page_source
            soup = BeautifulSoup(html, 'html.parser')

            # 查找包含该文章的链接
            article_links = soup.find_all('a', href=lambda x: x and article_id in str(x))
            if not article_links:
                article_path = article_url.replace('https://www.toutiao.com', '')
                article_links = soup.find_all('a', href=lambda x: x and article_path in str(x))

            if article_links:
                for link in article_links:
                    parent = link.parent
                    for _ in range(10):
                        if parent is None:
                            break
                        read_div = parent.find('div', class_='profile-feed-card-tools-text')
                        if not read_div:
                            read_div = parent.find('div', class_=lambda x: x and 'feed-card-tools' in str(x))
                        if read_div:
                            read_text = read_div.get_text(strip=True)
                            read_match = re.search(r'(\d+(?:\.\d+)?)([万]?)\s*阅读', read_text)
                            if read_match:
                                read_count = parse_number(read_match.group(1) + read_match.group(2))
                                shared.set_cache(cache_key, read_count)
                                return read_count
                        parent = parent.parent

            # 备选：全文搜索
            all_text = soup.get_text()
            pattern = rf'{article_id}.*?(\d+(?:\.\d+)?)([万]?)\s*阅读'
            match = re.search(pattern, all_text, re.DOTALL)
            if match:
                read_count = parse_number(match.group(1) + match.group(2))
                shared.set_cache(cache_key, read_count)
                return read_count

            return 0
        except Exception as e:
            logger.error(f"获取阅读数失败: {e}")
            return 0

    def get_article_details(self, article_url):
        """Selenium获取文章详情（点赞、评论、正文等）"""
        try:
            self.driver.get(article_url)
            time.sleep(random.uniform(2, 3))
            self.driver.execute_script("window.scrollBy(0, 500);")
            time.sleep(random.uniform(0.5, 1))

            html = self.driver.page_source
            soup = BeautifulSoup(html, 'html.parser')

            # 点赞数
            like_count = 0
            like_patterns = [
                r'点赞\s*(\d+)',
                r'(\d+)\s*点赞',
                r'likeCount["\']?\s*:\s*["\']?(\d+)',
                r'"digg_count"\s*:\s*(\d+)'
            ]
            for pattern in like_patterns:
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    like_count = int(match.group(1))
                    break
            if like_count == 0:
                text = soup.get_text()
                like_match = re.search(r'点赞\s*(\d+)', text)
                if like_match:
                    like_count = int(like_match.group(1))

            # 评论数
            comment_count = 0
            comment_patterns = [
                r'评论\s*(\d+)',
                r'(\d+)\s*评论',
                r'commentCount["\']?\s*:\s*["\']?(\d+)',
                r'"comment_count"\s*:\s*(\d+)'
            ]
            for pattern in comment_patterns:
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    comment_count = int(match.group(1))
                    break
            if comment_count == 0:
                text = soup.get_text()
                comment_match = re.search(r'评论\s*(\d+)', text)
                if comment_match:
                    comment_count = int(comment_match.group(1))

            # 正文和HTML
            content = ""
            article_html = ""
            content_selectors = [
                'article.syl-article-base',
                '.syl-article-base',
                'article',
                'div[class*="content"]',
                'div[class*="article-content"]'
            ]
            for selector in content_selectors:
                container = soup.select_one(selector)
                if container:
                    article_html = str(container)
                    paragraphs = container.find_all(['p', 'h1', 'h2', 'h3', 'h4'])
                    for p in paragraphs:
                        text = p.get_text(strip=True)
                        if text and len(text) > 10:
                            content += text + "\n\n"
                    break
            if not content:
                all_paragraphs = soup.find_all('p')
                for p in all_paragraphs:
                    text = p.get_text(strip=True)
                    if text and len(text) > 20:
                        content += text + "\n\n"

            # 标题
            title = ""
            title_selectors = [
                'h1',
                '.article-title',
                'div[class*="title"]',
                'title',
                'meta[property="og:title"]',
                'meta[name="title"]'
            ]
            for selector in title_selectors:
                elem = soup.select_one(selector)
                if elem:
                    if elem.name == 'meta':
                        title = elem.get('content', '').strip()
                    else:
                        title = elem.get_text(strip=True)
                    if title and len(title) > 5:
                        break
            if not title:
                page_title = soup.title.string if soup.title else ""
                if page_title:
                    title = re.sub(r'\s*[–—\-|]\s*今日头条.*$', '', page_title).strip()
            if not title:
                title = "无标题"

            return {
                'like_count': like_count,
                'comment_count': comment_count,
                'content': content.strip(),
                'content_length': len(content.strip()),
                'article_html': article_html,
                'title': title[:200]
            }
        except Exception as e:
            logger.error(f"Selenium获取文章详情失败 {article_url}: {e}")
            return {
                'like_count': 0,
                'comment_count': 0,
                'content': '',
                'content_length': 0,
                'article_html': '',
                'title': ''
            }

    def parse_article(self, article_info, filters):
        """解析一篇文章（粉丝数、阅读数、详情），返回结果字典"""
        article_id = article_info.get('article_id')
        url = article_info['url']
        author_url = article_info.get('author_url', '')
        valid_author = article_info.get('valid_author', False)

        result = {
            'url': url,
            'article_id': article_id,
            'author_url': author_url,
            'valid_author': valid_author,
            'publish_time': article_info.get('publish_time', '未知时间'),
            'title': article_info.get('title', ''),
            'author': article_info.get('author', ''),
            'channel': article_info.get('channel', ''),
            'read_count': 0,
            'comment_count': article_info.get('comment_count', 0),
            'fans_count': 0,
            'like_count': 0,
            'content': '',
            'content_length': 0,
            'article_html': '',
            'images': [],
            'image_count': 0,
            'filtered': False
        }

        try:
            # 时间过滤
            hours_ago = parse_hours_ago(result['publish_time'])
            if hours_ago is None or hours_ago > filters['max_hours']:
                result['error'] = f"时间超限: {hours_ago}"
                return result
            result['hours_ago'] = hours_ago

            # 粉丝数
            if valid_author and author_url:
                fans = self.get_author_fans_count(author_url)
                result['fans_count'] = fans
                if fans == 0 or fans > filters['max_fans']:
                    result['error'] = f"粉丝数 {fans} 不符"
                    return result
            else:
                result['error'] = "无效作者"
                return result

            # 阅读数
            read = self.get_article_read_count_exact(article_id, author_url, url)
            result['read_count'] = read

            # 详情
            details = self.get_article_details(url)
            result['like_count'] = details['like_count']
            if details['comment_count'] > 0:
                result['comment_count'] = details['comment_count']
            result['content'] = details['content']
            result['content_length'] = details['content_length']
            result['article_html'] = details['article_html']
            if details['title'] and details['title'] not in ('无标题', ''):
                result['title'] = details['title'][:200]

            # 过滤
            if (read >= filters['min_reads'] and
                result['like_count'] >= filters['min_likes'] and
                result['comment_count'] >= filters['min_comments']):
                result['filtered'] = True
            else:
                result['error'] = f"未通过过滤: 阅读{read}<{filters['min_reads']} or 点赞{result['like_count']}<{filters['min_likes']} or 评论{result['comment_count']}<{filters['min_comments']}"

            result['parse_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            return result
        except Exception as e:
            result['error'] = str(e)
            return result

    def close(self):
        try:
            if self.driver:
                self.driver.quit()
        except:
            pass


# ==================== 辅助函数（时间解析、数字解析、图片下载）====================
def parse_hours_ago(time_str):
    try:
        if not time_str or time_str == "未知时间":
            return None
        time_str = time_str.strip()
        now = datetime.now()

        if '小时前' in time_str:
            match = re.search(r'(\d+)\s*小时前', time_str)
            return int(match.group(1)) if match else None
        elif '分钟前' in time_str:
            match = re.search(r'(\d+)\s*分钟前', time_str)
            return int(match.group(1)) / 60 if match else None
        elif '天前' in time_str:
            match = re.search(r'(\d+)\s*天前', time_str)
            return int(match.group(1)) * 24 if match else None
        elif '今天' in time_str:
            match = re.search(r'今天\s*(\d+):(\d+)', time_str)
            if match:
                h, m = map(int, match.groups())
                t = now.replace(hour=h, minute=m, second=0)
                return (now - t).total_seconds() / 3600
            return 0
        elif '昨天' in time_str:
            match = re.search(r'昨天\s*(\d+):(\d+)', time_str)
            if match:
                h, m = map(int, match.groups())
                t = now.replace(hour=h, minute=m, second=0) - timedelta(days=1)
                return (now - t).total_seconds() / 3600
            return 24
        elif '前天' in time_str:
            match = re.search(r'前天\s*(\d+):(\d+)', time_str)
            if match:
                h, m = map(int, match.groups())
                t = now.replace(hour=h, minute=m, second=0) - timedelta(days=2)
                return (now - t).total_seconds() / 3600
            return 48
        elif '月' in time_str and '日' in time_str:
            match = re.search(r'(\d{1,2})月(\d{1,2})日', time_str)
            if match:
                m, d = map(int, match.groups())
                y = now.year
                if m > now.month:
                    y -= 1
                t = datetime(y, m, d)
                return (now - t).total_seconds() / 3600
        elif re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', time_str):
            match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', time_str)
            if match:
                y, m, d = map(int, match.groups())
                return (now - datetime(y, m, d)).total_seconds() / 3600
        return None
    except:
        return None


def parse_number(text):
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


def download_article_images(article_index, article_url, article_content, base_image_path):
    """下载图片（纯requests）"""
    import requests  # 确保导入
    try:
        date_folder = datetime.now().strftime("%Y%m%d")
        date_path = os.path.normpath(os.path.join(base_image_path, date_folder))
        os.makedirs(date_path, exist_ok=True)

        soup = BeautifulSoup(article_content, 'html.parser')
        img_tags = soup.find_all('img')
        image_paths = []
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': article_url
        }
        session = requests.Session()
        session.headers.update(headers)

        for i, img_tag in enumerate(img_tags, 1):
            img_url = None
            for attr in ['data-src', 'src', 'data-original', 'original-src']:
                if img_tag.get(attr):
                    img_url = img_tag.get(attr)
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    elif not img_url.startswith('http'):
                        img_url = urljoin(article_url, img_url)
                    break

            if not img_url or not img_url.startswith('http'):
                continue

            try:
                resp = session.get(img_url, timeout=10, stream=True)
                if resp.status_code == 200:
                    content_type = resp.headers.get('Content-Type', '')
                    ext = '.jpg'
                    if 'png' in content_type:
                        ext = '.png'
                    elif 'gif' in content_type:
                        ext = '.gif'
                    elif 'webp' in content_type:
                        ext = '.webp'

                    filename = f"{article_index}.{i}{ext}"
                    filepath = os.path.join(date_path, filename)
                    with open(filepath, 'wb') as f:
                        for chunk in resp.iter_content(8192):
                            f.write(chunk)

                    image_paths.append({
                        'index': f"{article_index}.{i}",
                        'url': img_url,
                        'local_path': filepath,
                        'size': os.path.getsize(filepath),
                        'filename': filename
                    })
            except Exception as e:
                logger.warning(f"下载图片失败: {e}")

        return image_paths
    except Exception as e:
        logger.error(f"下载图片异常: {e}")
        return []


# ==================== GUI界面 =====================
class MultiBrowserCrawlerGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("今日头条爬虫 - 5列表+2详情浏览器")
        self.root.geometry("1400x900")

        self.title_font = ('微软雅黑', 16, 'bold')
        self.label_font = ('微软雅黑', 11)
        self.stats_font = ('微软雅黑', 10)

        # 配置变量
        self.selected_channel = tk.StringVar(value="推荐")
        self.output_path = tk.StringVar(value=os.getcwd())
        self.image_base_path = tk.StringVar(value=r"C:\Users\Administrator\Desktop\头条爆文\图片")

        self.max_fans = tk.StringVar(value="7000")
        self.min_reads = tk.StringVar(value="3000")
        self.max_hours = tk.StringVar(value="10")
        self.min_likes = tk.StringVar(value="0")
        self.min_comments = tk.StringVar(value="0")

        self.target_articles = tk.StringVar(value="100")   # 目标文章总数
        self.num_producers = tk.StringVar(value="5")       # 列表浏览器数量
        self.num_consumers = tk.StringVar(value="2")       # 详情浏览器数量

        self.enable_cache = tk.BooleanVar(value=True)
        self.download_images = tk.BooleanVar(value=True)

        # 状态变量
        self.is_crawling = False
        self.stop_requested = False
        self.filtered_articles = []          # 最终通过过滤的文章列表
        self.article_data_dict = {}           # tree item -> article
        self.selected_items = set()

        # 进度显示
        self.progress_var = tk.DoubleVar()
        self.status_var = tk.StringVar(value="就绪")
        self.scroll_status_var = tk.StringVar(value="滚动次数: 0")
        self.batch_status_var = tk.StringVar(value="已获取: 0 / 待解析: 0")
        self.success_rate_var = tk.StringVar(value="粉丝: 0% | 阅读: 0% | 点赞: 0%")
        self.stats_label_text = tk.StringVar(value="总计: 0 篇 | 符合条件: 0 篇")
        self.cache_hits_var = tk.StringVar(value="缓存命中: 0")

        self.create_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(7, weight=1)

        # 标题
        title_frame = ttk.Frame(main_frame)
        title_frame.grid(row=0, column=0, columnspan=6, sticky=(tk.W, tk.E), pady=(0, 10))
        ttk.Label(title_frame, text="今日头条爬虫 - 5列表浏览器 + 2详情浏览器",
                  font=self.title_font, foreground='darkblue').pack(side=tk.LEFT)

        # 配置行
        config_frame = ttk.LabelFrame(main_frame, text="配置设置", padding="10")
        config_frame.grid(row=1, column=0, columnspan=6, sticky=(tk.W, tk.E), pady=(0, 10))

        ttk.Label(config_frame, text="文章频道:").grid(row=0, column=0, padx=(0,5))
        channel_combo = ttk.Combobox(config_frame, textvariable=self.selected_channel,
                                     values=['推荐', '财经', '科技', '热点', '国际', '军事', '体育', '娱乐', '历史',
                                             '美食', '旅游'],
                                     state="readonly", width=12)
        channel_combo.grid(row=0, column=1, padx=(0,20))

        ttk.Label(config_frame, text="目标文章数:").grid(row=0, column=2, padx=(0,5))
        ttk.Entry(config_frame, textvariable=self.target_articles, width=8).grid(row=0, column=3, padx=(0,20))

        ttk.Label(config_frame, text="列表浏览器:").grid(row=0, column=4, padx=(0,5))
        ttk.Entry(config_frame, textvariable=self.num_producers, width=5).grid(row=0, column=5, padx=(0,20))

        ttk.Label(config_frame, text="详情浏览器:").grid(row=0, column=6, padx=(0,5))
        ttk.Entry(config_frame, textvariable=self.num_consumers, width=5).grid(row=0, column=7, padx=(0,20))

        ttk.Checkbutton(config_frame, text="启用缓存", variable=self.enable_cache).grid(row=0, column=8, padx=(0,10))
        ttk.Checkbutton(config_frame, text="下载图片", variable=self.download_images).grid(row=0, column=9)

        # 过滤条件
        filter_frame = ttk.LabelFrame(main_frame, text="过滤条件", padding="10")
        filter_frame.grid(row=2, column=0, columnspan=6, sticky=(tk.W, tk.E), pady=(0, 10))

        filters = [
            ("粉丝数 ≤", self.max_fans, "个"),
            ("阅读量 ≥", self.min_reads, "次"),
            ("发布时间 ≤", self.max_hours, "小时"),
            ("点赞数 ≥", self.min_likes, "个"),
            ("评论数 ≥", self.min_comments, "条")
        ]
        for i, (label, var, unit) in enumerate(filters):
            ttk.Label(filter_frame, text=label).grid(row=0, column=i*3, padx=(10,5), pady=5)
            ttk.Entry(filter_frame, textvariable=var, width=8).grid(row=0, column=i*3+1, padx=(0,5), pady=5)
            ttk.Label(filter_frame, text=unit).grid(row=0, column=i*3+2, padx=(0,20), pady=5)

        # 输出设置
        output_frame = ttk.LabelFrame(main_frame, text="输出设置", padding="10")
        output_frame.grid(row=3, column=0, columnspan=6, sticky=(tk.W, tk.E), pady=(0, 10))

        ttk.Label(output_frame, text="输出路径:").grid(row=0, column=0, padx=(0,10), sticky=tk.W)
        ttk.Entry(output_frame, textvariable=self.output_path, width=60).grid(row=0, column=1, padx=(0,10))
        ttk.Button(output_frame, text="浏览", command=self.browse_output_path, width=8).grid(row=0, column=2)

        ttk.Label(output_frame, text="图片路径:").grid(row=1, column=0, padx=(0,10), sticky=tk.W, pady=(5,0))
        ttk.Entry(output_frame, textvariable=self.image_base_path, width=60).grid(row=1, column=1, padx=(0,10), pady=(5,0))
        ttk.Button(output_frame, text="浏览", command=self.browse_image_path, width=8).grid(row=1, column=2, pady=(5,0))

        # 控制按钮
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=4, column=0, columnspan=6, pady=(0,15))

        self.start_btn = ttk.Button(btn_frame, text="开始爬取", command=self.start_crawling, width=12)
        self.start_btn.grid(row=0, column=0, padx=(0,10))

        self.stop_btn = ttk.Button(btn_frame, text="停止爬取", command=self.stop_crawling,
                                   width=12, state='disabled')
        self.stop_btn.grid(row=0, column=1, padx=(0,10))

        ttk.Button(btn_frame, text="导出结果", command=self.export_results, width=12).grid(row=0, column=2, padx=(0,10))
        ttk.Button(btn_frame, text="清空数据", command=self.clear_data, width=12).grid(row=0, column=3, padx=(0,10))
        ttk.Button(btn_frame, text="二次过滤", command=self.refilter_articles, width=12).grid(row=0, column=4)

        # 进度条
        progress_frame = ttk.Frame(main_frame)
        progress_frame.grid(row=5, column=0, columnspan=6, sticky=(tk.W, tk.E), pady=(0,10))
        progress_frame.columnconfigure(0, weight=1)

        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=(0,10))
        self.status_label = ttk.Label(progress_frame, textvariable=self.status_var, font=self.stats_font)
        self.status_label.grid(row=0, column=1)

        # 统计行
        stats_frame = ttk.Frame(main_frame)
        stats_frame.grid(row=6, column=0, columnspan=6, sticky=(tk.W, tk.E), pady=(0,10))

        ttk.Label(stats_frame, textvariable=self.batch_status_var, font=self.stats_font).grid(row=0, column=0, padx=(0,20))
        ttk.Label(stats_frame, textvariable=self.scroll_status_var, font=self.stats_font).grid(row=0, column=1, padx=(0,20))
        ttk.Label(stats_frame, textvariable=self.success_rate_var, font=self.stats_font, foreground='darkgreen').grid(row=0, column=2, padx=(0,20))
        ttk.Label(stats_frame, textvariable=self.stats_label_text, font=self.stats_font).grid(row=0, column=3, padx=(0,20))
        ttk.Label(stats_frame, textvariable=self.cache_hits_var, font=self.stats_font).grid(row=0, column=4)

        # 文章预览表格
        preview_frame = ttk.LabelFrame(main_frame, text="文章预览", padding="10")
        preview_frame.grid(row=7, column=0, columnspan=6, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0,10))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        columns = ('选择', '序号', '标题', '作者', '粉丝数', '阅读量', '点赞', '评论', '发布时间', '状态')
        self.tree = ttk.Treeview(preview_frame, columns=columns, show='headings', height=15)

        col_config = [
            ('选择', 50, 'center'),
            ('序号', 50, 'center'),
            ('标题', 250, 'w'),
            ('作者', 100, 'center'),
            ('粉丝数', 80, 'center'),
            ('阅读量', 80, 'center'),
            ('点赞', 60, 'center'),
            ('评论', 60, 'center'),
            ('发布时间', 120, 'center'),
            ('状态', 50, 'center')
        ]
        for col, width, anchor in col_config:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=width, anchor=anchor)

        scroll_y = ttk.Scrollbar(preview_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scroll_x = ttk.Scrollbar(preview_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

        self.tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scroll_y.grid(row=0, column=1, sticky=(tk.N, tk.S))
        scroll_x.grid(row=1, column=0, sticky=(tk.W, tk.E))

        self.tree.bind('<Button-1>', self.on_tree_click)
        self.tree.bind('<Double-1>', self.on_item_double_click)

    # ---------- 辅助方法 ----------
    def browse_output_path(self):
        path = filedialog.askdirectory()
        if path:
            self.output_path.set(path)

    def browse_image_path(self):
        path = filedialog.askdirectory()
        if path:
            self.image_base_path.set(os.path.normpath(path))

    def validate_image_path(self):
        path = self.image_base_path.get()
        if not path:
            return False
        try:
            os.makedirs(path, exist_ok=True)
            return True
        except:
            return False

    # ---------- 爬虫控制 ----------
    def start_crawling(self):
        if self.is_crawling:
            return

        # 验证图片路径
        if self.download_images.get():
            if not self.image_base_path.get():
                messagebox.showwarning("警告", "请设置图片保存路径")
                return
            if not self.validate_image_path():
                messagebox.showerror("错误", "图片路径无效")
                return

        # 重置状态
        self.is_crawling = True
        self.stop_requested = False
        self.filtered_articles = []
        self.article_data_dict.clear()
        self.selected_items.clear()
        self.progress_var.set(0)
        self.status_var.set("启动中...")
        self.scroll_status_var.set("滚动次数: 0")
        self.batch_status_var.set("已获取: 0 / 待解析: 0")
        self.success_rate_var.set("粉丝: 0% | 阅读: 0% | 点赞: 0%")
        self.stats_label_text.set("总计: 0 篇 | 符合条件: 0 篇")
        self.cache_hits_var.set("缓存命中: 0")
        for item in self.tree.get_children():
            self.tree.delete(item)

        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')

        # 启动后台线程
        self.crawl_thread = threading.Thread(target=self.run_crawler, daemon=True)
        self.crawl_thread.start()

    def stop_crawling(self):
        self.stop_requested = True
        self.status_var.set("正在停止...")
        self.stop_btn.config(state='disabled')

    def run_crawler(self):
        """主爬虫逻辑：启动N个列表浏览器 + M个详情浏览器"""
        try:
            channel_map = {
                '推荐': 'recommend', '财经': 'news_finance', '科技': 'news_tech',
                '热点': 'news_hot', '国际': 'news_world', '军事': 'news_military',
                '体育': 'news_sports', '娱乐': 'news_entertainment', '历史': 'news_history',
                '美食': 'news_food', '旅游': 'news_travel'
            }
            channel_slug = channel_map.get(self.selected_channel.get(), 'recommend')
            target_total = int(self.target_articles.get() or 100)
            max_fans = int(self.max_fans.get() or 7000)
            min_reads = int(self.min_reads.get() or 3000)
            max_hours = int(self.max_hours.get() or 17)
            min_likes = int(self.min_likes.get() or 0)
            min_comments = int(self.min_comments.get() or 0)

            filters = {
                'max_fans': max_fans,
                'min_reads': min_reads,
                'max_hours': max_hours,
                'min_likes': min_likes,
                'min_comments': min_comments
            }

            num_producers = int(self.num_producers.get() or 5)
            num_consumers = int(self.num_consumers.get() or 2)

            # 创建队列
            article_queue = queue.Queue()          # 待解析的文章
            result_queue = queue.Queue()            # 解析完成的结果

            # 共享停止事件
            stop_event = threading.Event()

            # 启动生产者线程 (列表浏览器)
            producers = []
            for i in range(num_producers):
                crawler = ListCrawler(headless=False)
                t = threading.Thread(target=self.producer_task,
                                     args=(crawler, channel_slug, article_queue, stop_event, filters),
                                     name=f"Producer-{i+1}")
                t.daemon = True
                t.start()
                producers.append((t, crawler))

            # 启动消费者线程 (详情浏览器)
            consumers = []
            for i in range(num_consumers):
                crawler = DetailCrawler(headless=False)
                t = threading.Thread(target=self.consumer_task,
                                     args=(crawler, article_queue, result_queue, stop_event, filters),
                                     name=f"Consumer-{i+1}")
                t.daemon = True
                t.start()
                consumers.append((t, crawler))

            # 主线程监控队列并更新UI
            processed_count = 0
            while processed_count < target_total and not stop_event.is_set():
                try:
                    result = result_queue.get(timeout=0.5)
                    processed_count += 1
                    self.filtered_articles.append(result)
                    self.root.after(0, self.update_preview_item, result, processed_count)
                    self.root.after(0, self.update_stats)
                except queue.Empty:
                    pass

                all_producers_done = all(not t.is_alive() for t, _ in producers)
                if all_producers_done and article_queue.empty() and result_queue.empty():
                    break

                progress = min(processed_count / target_total * 100, 99)
                self.root.after(0, lambda p=progress: self.progress_var.set(p))
                self.root.after(0, lambda p=processed_count, q=article_queue.qsize():
                                self.batch_status_var.set(f"已获取: {p} / 待解析: {q}"))

            stop_event.set()

            for t, crawler in producers:
                t.join(timeout=5)
                crawler.close()
            for t, crawler in consumers:
                t.join(timeout=5)
                crawler.close()

            self.root.after(0, self.on_crawl_finished, processed_count)

        except Exception as e:
            logger.error(f"爬虫主线程异常: {e}")
            self.root.after(0, lambda: messagebox.showerror("错误", f"爬虫异常: {str(e)}"))
        finally:
            self.root.after(0, self.reset_ui_after_crawl)

    def producer_task(self, crawler, channel_slug, article_queue, stop_event, filters):
        """生产者：使用浏览器获取文章列表，放入队列"""
        while not stop_event.is_set():
            try:
                articles = crawler.get_article_links_from_channel(
                    channel_slug,
                    target_count=20,
                    max_hours=filters['max_hours'],
                    min_comments=filters['min_comments']
                )
                for art in articles:
                    if stop_event.is_set():
                        break
                    article_queue.put(art)
                if len(articles) == 0:
                    time.sleep(2)
            except Exception as e:
                logger.error(f"生产者异常: {e}")
                time.sleep(2)

    def consumer_task(self, crawler, article_queue, result_queue, stop_event, filters):
        """消费者：使用详情浏览器解析文章"""
        while not stop_event.is_set():
            try:
                article = article_queue.get(timeout=2)
            except queue.Empty:
                continue

            if stop_event.is_set():
                break

            try:
                result = crawler.parse_article(article, filters)
                if result.get('filtered'):
                    result_queue.put(result)
            except Exception as e:
                logger.error(f"消费者解析失败: {e}")

    def on_crawl_finished(self, processed_count):
        self.progress_var.set(100)
        self.status_var.set(f"完成！共获取 {processed_count} 篇符合条件的文章")
        if self.filtered_articles:
            self.auto_export()

    def reset_ui_after_crawl(self):
        self.is_crawling = False
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')

    # ---------- UI 更新 ----------
    def update_preview_item(self, article, index):
        read_disp = self.format_number(article.get('read_count', 0))
        fans_disp = self.format_number(article.get('fans_count', 0))
        like_disp = self.format_number(article.get('like_count', 0))
        img_cnt = article.get('image_count', 0)

        title = article.get('title', '')
        title_disp = title[:27] + '...' if len(title) > 30 else title
        author = article.get('author', '')
        author_disp = author[:7] + '...' if len(author) > 10 else author
        status = f"✓({img_cnt})" if img_cnt > 0 else "✓"

        item_id = self.tree.insert('', 'end',
                                   values=('', index, title_disp, author_disp, fans_disp, read_disp, like_disp,
                                           article.get('comment_count', 0), article.get('publish_time', '')[:16],
                                           status))
        self.tree.set(item_id, column='选择', value='☑')
        self.selected_items.add(item_id)
        self.article_data_dict[item_id] = article

    def update_stats(self):
        total = len(self.filtered_articles)
        self.stats_label_text.set(f"总计: {total} 篇 | 符合条件: {total} 篇")

    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        column = self.tree.identify_column(event.x)
        if column == '#1':
            item = self.tree.identify_row(event.y)
            if item:
                current = self.tree.set(item, column='选择')
                if current == '☑':
                    self.tree.set(item, column='选择', value='□')
                    self.selected_items.discard(item)
                else:
                    self.tree.set(item, column='选择', value='☑')
                    self.selected_items.add(item)

    def on_item_double_click(self, event):
        sel = self.tree.selection()
        if sel:
            item = sel[0]
            article = self.article_data_dict.get(item)
            if article and 'url' in article:
                webbrowser.open(article['url'])

    def format_number(self, num):
        if num is None:
            return "0"
        try:
            num = int(num)
            if num >= 100000000:
                return f"{num/100000000:.2f}亿"
            elif num >= 10000:
                return f"{num/10000:.1f}万"
            elif num >= 1000:
                return f"{num/1000:.1f}千"
            else:
                return str(num)
        except:
            return str(num)

    def auto_export(self):
        """自动导出结果（包含图片下载）"""
        if not self.filtered_articles:
            return
        try:
            if self.download_images.get():
                self.status_var.set("正在下载图片...")
                self.root.update()
                self._download_all_images()

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            channel = self.selected_channel.get()
            safe_channel = re.sub(r'[\\/*?:"<>|]', '', channel)
            out_dir = self.output_path.get() or os.getcwd()

            excel_path = os.path.join(out_dir, f"toutiao_mixed_{safe_channel}_{timestamp}.xlsx")
            txt_dir = os.path.join(out_dir, f"txt_mixed_{safe_channel}_{timestamp}")
            os.makedirs(txt_dir, exist_ok=True)

            df_data = []
            for article in self.filtered_articles:
                image_info = []
                for img in article.get('images', []):
                    idx = img.get('index', '')
                    fname = img.get('filename', '')
                    size = img.get('size', 0)
                    if idx and fname:
                        image_info.append(f"{idx}: {fname} ({size/1024:.1f}KB)")

                row = {
                    '标题': article.get('title', ''),
                    '作者': article.get('author', ''),
                    '粉丝数': article.get('fans_count', 0),
                    '阅读量': article.get('read_count', 0),
                    '点赞数': article.get('like_count', 0),
                    '评论数': article.get('comment_count', 0),
                    '发布时间': article.get('publish_time', ''),
                    '文章链接': article.get('url', ''),
                    '作者主页': article.get('author_url', ''),
                    '频道': article.get('channel', ''),
                    '获取时间': article.get('parse_time', ''),
                    '发布时间差(小时)': article.get('hours_ago', 0),
                    '内容长度': article.get('content_length', 0),
                    '图片数量': article.get('image_count', 0),
                    '图片列表': '; '.join(image_info) if image_info else '',
                }
                df_data.append(row)

            pd.DataFrame(df_data).to_excel(excel_path, index=False)

            for i, article in enumerate(self.filtered_articles, 1):
                title = article.get('title', f'文章_{i}')
                safe_title = re.sub(r'[\\/*?:"<>|]', '', title)
                filename = f"{i:03d}_{safe_title[:30]}.txt"
                filepath = os.path.join(txt_dir, filename)

                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(f"标题: {title}\n")
                    f.write(f"作者: {article.get('author', '')}\n")
                    f.write(f"发布时间: {article.get('publish_time', '')}\n")
                    f.write(f"文章链接: {article.get('url', '')}\n")
                    f.write(f"作者主页: {article.get('author_url', '')}\n")
                    f.write(f"粉丝数: {self.format_number(article.get('fans_count', 0))}\n")
                    f.write(f"阅读量: {self.format_number(article.get('read_count', 0))}\n")
                    f.write(f"点赞数: {article.get('like_count', 0)}\n")
                    f.write(f"评论数: {article.get('comment_count', 0)}\n")
                    f.write(f"图片数量: {article.get('image_count', 0)}\n")

                    images = article.get('images', [])
                    if images:
                        f.write("\n图片列表:\n")
                        for img in images:
                            f.write(f"  {img.get('index', '')}: {img.get('filename', '')} ({img.get('size', 0)/1024:.1f}KB)\n")
                            f.write(f"    路径: {img.get('local_path', '')}\n")

                    f.write(f"\n内容长度: {article.get('content_length', 0)} 字符\n")
                    f.write("\n" + "="*60 + "\n\n")
                    f.write(article.get('content', ''))

            logger.info(f"自动导出成功: {excel_path}")
            messagebox.showinfo("导出完成", f"结果已保存至:\n{excel_path}\n{txt_dir}")
        except Exception as e:
            logger.error(f"自动导出失败: {e}")
            messagebox.showerror("导出错误", str(e))

    def _download_all_images(self):
        """并发下载所有文章的图片"""
        if not self.filtered_articles:
            return
        total = len(self.filtered_articles)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_index = {}
            for i, article in enumerate(self.filtered_articles, 1):
                if not article.get('article_html'):
                    continue
                future = executor.submit(
                    download_article_images,
                    i,
                    article['url'],
                    article['article_html'],
                    self.image_base_path.get()
                )
                future_to_index[future] = (i, article)

            completed = 0
            for future in concurrent.futures.as_completed(future_to_index):
                idx, article = future_to_index[future]
                try:
                    images = future.result()
                    article['images'] = images
                    article['image_count'] = len(images)
                except Exception as e:
                    logger.error(f"下载文章 {idx} 图片失败: {e}")
                completed += 1
                self.status_var.set(f"图片下载: {completed}/{total}")
                self.root.update()
        logger.info("所有图片下载完成")

    def export_results(self):
        if not self.filtered_articles:
            messagebox.showwarning("警告", "没有数据可导出")
            return
        self.auto_export()

    def refilter_articles(self):
        if not self.filtered_articles:
            return
        try:
            max_fans = int(self.max_fans.get() or 7000)
            min_reads = int(self.min_reads.get() or 3000)
            max_hours = int(self.max_hours.get() or 17)
            min_likes = int(self.min_likes.get() or 0)
            min_comments = int(self.min_comments.get() or 0)
        except ValueError:
            messagebox.showerror("错误", "过滤条件必须是数字")
            return

        new_list = []
        for art in self.filtered_articles:
            hours = art.get('hours_ago', 999)
            if (art.get('fans_count', 0) <= max_fans and
                art.get('read_count', 0) >= min_reads and
                hours <= max_hours and
                art.get('like_count', 0) >= min_likes and
                art.get('comment_count', 0) >= min_comments):
                new_list.append(art)

        self.filtered_articles = new_list
        self.update_preview_after_refilter()
        messagebox.showinfo("二次过滤", f"保留 {len(new_list)} 篇")

    def update_preview_after_refilter(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.article_data_dict.clear()
        self.selected_items.clear()
        for i, art in enumerate(self.filtered_articles, 1):
            self.update_preview_item(art, i)

    def clear_data(self):
        if messagebox.askyesno("确认", "清空所有数据？"):
            self.filtered_articles = []
            self.article_data_dict.clear()
            self.selected_items.clear()
            for item in self.tree.get_children():
                self.tree.delete(item)
            self.progress_var.set(0)
            self.status_var.set("就绪")
            self.batch_status_var.set("已获取: 0 / 待解析: 0")
            self.stats_label_text.set("总计: 0 篇 | 符合条件: 0 篇")

    def on_closing(self):
        if self.is_crawling:
            if messagebox.askyesno("确认", "爬虫正在运行，确定退出？"):
                self.stop_requested = True
                time.sleep(1)
        shared.save_cache()
        shared.save_blacklist()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = MultiBrowserCrawlerGUI()
    app.run()