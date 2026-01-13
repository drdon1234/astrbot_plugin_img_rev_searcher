"""Google Lens搜索请求类 - 基于requests库"""
import os
import time
import urllib.parse
import asyncio
from pathlib import Path
from typing import Any, Literal, Optional, Union
from pyquery import PyQuery
from ..response_parser import GoogleLensExactMatchesResponse, GoogleLensResponse
from ..ext_tools import read_file, parse_html
from ..types import FileContent
from ..google_config import GoogleConfig
from ..google_cookie_getter import GoogleCookieGetter
import requests


VALID_SEARCH_TYPES = ["all", "products", "visual_matches", "exact_matches"]
SEARCH_TYPE_UDM = {
    "products": "37",
    "visual_matches": "44",
    "exact_matches": "48"
}

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'Referer': 'https://lens.google.com/',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1'
}


class GoogleLens:
    """
    Google Lens搜索请求类 - 基于requests库
    
    提供基于图像的搜索功能，支持多种搜索类型和过滤选项
    """
    
    def __init__(
        self,
        base_url: str = "https://lens.google.com",
        search_url: str = "https://www.google.com",
        search_type: Literal["all", "products", "visual_matches", "exact_matches"] = "all",
        q: Optional[str] = None,
        hl: str = "en",
        country: str = "US",
        max_results: int = 50,
        cookies: Optional[str] = None,
        proxies: Optional[str] = None,
        timeout: float = 30,
        auto_fetch_cookie: bool = False,
        cookie_update_time: Optional[float] = None,
        cookie_expire_hours: int = 24,
        use_remote_browser: bool = False,
        remote_browser_url: Optional[str] = None,
        **kwargs: Any,
    ):
        """
        初始化Google Lens搜索请求
        
        参数:
            base_url: Google Lens API基础URL
            search_url: Google搜索基础URL
            search_type: 搜索类型
            q: 搜索查询词
            hl: 界面语言代码
            country: 国家/地区代码
            max_results: 最大结果数量
            cookies: Cookie字符串
            proxies: 代理服务器地址
            timeout: 请求超时时间(秒)
            auto_fetch_cookie: 是否自动获取cookie
            cookie_update_time: Cookie更新时间戳
            cookie_expire_hours: Cookie过期小时数
            use_remote_browser: 是否使用远程浏览器
            remote_browser_url: 远程浏览器地址
            **kwargs: 其他请求参数
        """
        self.base_url = base_url
        if search_type not in VALID_SEARCH_TYPES:
            raise ValueError(f"无效的search_type: {search_type}。必须是以下之一: {', '.join(VALID_SEARCH_TYPES)}")
        if search_type == "exact_matches" and q:
            raise ValueError("Query parameter 'q' is not applicable for 'exact_matches' search_type.")
        if max_results <= 0:
            raise ValueError("max_results must be a positive integer")
        
        self.search_url: str = search_url
        self.hl_param: str = f"{hl}-{country.upper()}"
        self.search_type: str = search_type
        self.q: Optional[str] = q
        self.max_results: int = max_results
        self.timeout: float = timeout
        
        # 初始化配置和会话
        config_dict = {
            "cookie": cookies or "",
            "auto_fetch_cookie": auto_fetch_cookie,
            "cookie_update_time": cookie_update_time,
            "cookie_expire_hours": cookie_expire_hours,
            "use_remote_browser": use_remote_browser,
            "remote_browser_url": remote_browser_url or "http://localhost:4444/wd/hub",
            "proxies": proxies
        }
        self.config = GoogleConfig(config_dict=config_dict)
        self.session: Optional[requests.Session] = None
        self.cookies: dict = {}
        self._init_cookies()
    
    def _init_cookies(self):
        """初始化cookie"""
        cookie_str = self.config.get('cookie')
        if cookie_str:
            self.cookies = GoogleConfig.cookie_string_to_dict(cookie_str)
        else:
            self.cookies = {}
    
    async def _ensure_session(self):
        """确保有可用的 requests 会话"""
        if self.session is None:
            self.session = requests.Session()
        
        # 配置代理（如果配置了）
        proxy = self.config.get('proxies')
        if proxy:
            if not proxy.startswith(('http://', 'https://')):
                proxy = f"http://{proxy}"
            if self.session.proxies.get('http') != proxy:
                self.session.proxies = {
                    'http': proxy,
                    'https': proxy
                }
                print(f"[INFO] 使用代理: {proxy}")
    
    async def _get_cookie(self):
        """获取cookie（异步执行同步的 CookieGetter）"""
        cookie_getter = GoogleCookieGetter(
            use_remote_browser=self.config.get('use_remote_browser', False),
            remote_browser_url=self.config.get('remote_browser_url')
        )
        
        try:
            cookie_result = await asyncio.to_thread(cookie_getter.get_cookie)
            
            if cookie_result and cookie_result.get('cookie'):
                cookie_str = cookie_result['cookie']
                self.cookies = GoogleConfig.cookie_string_to_dict(cookie_str)
                self.config.set('cookie', cookie_str)
                self.config.set('cookie_update_time', time.time())
                print(f"[INFO] Cookie已保存")
            
            return cookie_result
        finally:
            await asyncio.to_thread(cookie_getter.close)
    
    async def _perform_image_search(
        self,
        url: Optional[str] = None,
        file: FileContent = None,
        q: Optional[str] = None,
    ) -> dict:
        """
        执行图像搜索请求
        
        参数:
            url: 图像URL
            file: 本地文件内容
            q: 搜索查询词
            
        返回:
            dict: 包含text、url、status_code的响应字典
        """
        await self._ensure_session()
        
        # 检查是否需要自动获取 cookie
        if self.config.get('auto_fetch_cookie', False):
            cookie_str = self.config.get('cookie')
            if not cookie_str or self.config.is_cookie_expired():
                use_remote = self.config.get('use_remote_browser', False)
                remote_url = self.config.get('remote_browser_url')
                browser_type = f"远程浏览器 ({remote_url})" if use_remote else "本地浏览器"
                print(f"[INFO] 正在使用{browser_type}获取Cookie...")
                cookie_result = await self._get_cookie()
                if cookie_result and cookie_result.get('cookie'):
                    print(f"[INFO] Cookie获取成功")
                else:
                    print(f"[WARN] Cookie获取失败")
        
        params = {"hl": self.hl_param}
        if q and self.search_type != "exact_matches":
            params["q"] = q
        
        cookies_to_use = self.cookies if self.cookies else None
        
        try:
            if file:
                endpoint = f"{self.base_url}/v3/upload"
                filename = "image.jpg" if isinstance(file, bytes) else Path(file).name
                ext = os.path.splitext(filename)[1].lower()
                mime_map = {
                    '.jpg': 'image/jpeg',
                    '.jpeg': 'image/jpeg',
                    '.png': 'image/png',
                    '.gif': 'image/gif',
                    '.webp': 'image/webp',
                    '.bmp': 'image/bmp'
                }
                mime_type = mime_map.get(ext, 'image/jpeg')
                
                def upload_file():
                    file_data = read_file(file)
                    files = {"encoded_image": (filename, file_data, mime_type)}
                    return self.session.post(
                        endpoint,
                        headers=DEFAULT_HEADERS,
                        cookies=cookies_to_use,
                        params=params,
                        files=files,
                        allow_redirects=True,
                        timeout=self.timeout
                    )
                
                response = await asyncio.to_thread(upload_file)
                
                # 如果上传成功，可能需要获取精确匹配URL
                if response.status_code == 200 and self.search_type != "all":
                    result_url = response.url
                    parsed = urllib.parse.urlparse(result_url)
                    
                    if self.search_type in SEARCH_TYPE_UDM:
                        udm_value = SEARCH_TYPE_UDM[self.search_type]
                        if parsed.query:
                            query_params = urllib.parse.parse_qs(parsed.query)
                            query_params['ep'] = ['mecb']
                            query_params['udm'] = [udm_value]
                            new_query = urllib.parse.urlencode(query_params, doseq=True)
                            exact_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{new_query}"
                        else:
                            exact_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?ep=mecb&udm={udm_value}"
                        
                        def get_exact_match():
                            return self.session.get(
                                exact_url,
                                headers=DEFAULT_HEADERS,
                                cookies=cookies_to_use,
                                allow_redirects=True,
                                timeout=self.timeout
                            )
                        
                        response = await asyncio.to_thread(get_exact_match)
                
            elif url:
                if self.search_type == "exact_matches":
                    # 对于精确匹配，使用 uploadbyurl 并添加 ep=mecb 参数
                    encoded_url = urllib.parse.quote(url, safe='')
                    endpoint = f"{self.base_url}/uploadbyurl"
                    params["url"] = encoded_url
                    params["ep"] = "mecb"
                else:
                    encoded_url = urllib.parse.quote(url, safe='')
                    endpoint = f"{self.base_url}/uploadbyurl"
                    params["url"] = encoded_url
                
                def get_by_url():
                    return self.session.get(
                        endpoint,
                        headers=DEFAULT_HEADERS,
                        cookies=cookies_to_use,
                        params=params,
                        allow_redirects=True,
                        timeout=self.timeout
                    )
                
                response = await asyncio.to_thread(get_by_url)
                
                # 如果不是精确匹配，检查是否需要获取特定类型的链接
                if self.search_type != "all" and self.search_type != "exact_matches":
                    dom = parse_html(response.text)
                    exact_link = ""
                    if self.search_type in SEARCH_TYPE_UDM:
                        udm_value = SEARCH_TYPE_UDM[self.search_type]
                        exact_link = dom(f'a[href*="udm={udm_value}"]').attr("href") or ""
                    
                    if exact_link:
                        def get_exact_match():
                            return self.session.get(
                                f"{self.search_url}{exact_link}",
                                headers=DEFAULT_HEADERS,
                                cookies=cookies_to_use,
                                allow_redirects=True,
                                timeout=self.timeout
                            )
                        response = await asyncio.to_thread(get_exact_match)
            else:
                raise ValueError("Either 'url' or 'file' must be provided")
            
            # 修复编码问题
            if response.encoding is None or response.encoding.lower() in ['iso-8859-1', 'latin-1']:
                response.encoding = 'utf-8'
            
            return {
                'text': response.text,
                'url': str(response.url),
                'status_code': response.status_code
            }
        except Exception as e:
            return {
                'text': '',
                'url': url or '',
                'status_code': 0,
                'error': str(e)
            }
    
    async def search(
        self,
        url: Optional[str] = None,
        file: FileContent = None,
        q: Optional[str] = None,
        **kwargs: Any,
    ) -> Union[GoogleLensResponse, GoogleLensExactMatchesResponse]:
        """
        执行Google Lens搜索
        
        参数:
            url: 图像URL
            file: 本地文件内容
            q: 搜索查询词，对于exact_matches类型会被忽略
            **kwargs: 其他搜索参数
            
        返回:
            Union[GoogleLensResponse, GoogleLensExactMatchesResponse]: 根据搜索类型返回相应的响应对象
        """
        if q is not None and self.search_type == "exact_matches":
            q = None
        
        resp = await self._perform_image_search(url, file, q)
        
        if self.search_type == "exact_matches":
            response = GoogleLensExactMatchesResponse(resp['text'], resp['url'])
            response._parse_response(resp['text'], resp_url=resp['url'], max_results=self.max_results)
            return response
        else:
            response = GoogleLensResponse(resp['text'], resp['url'])
            response._parse_response(resp['text'], resp_url=resp['url'], max_results=self.max_results)
            return response
    
    async def close(self):
        """关闭客户端和会话"""
        if self.session:
            self.session.close()
            self.session = None
