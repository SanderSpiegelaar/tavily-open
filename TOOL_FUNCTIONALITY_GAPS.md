# TrailSearch 工具属性功能差距分析

**聚焦：** 纯工具能力，不考虑商业化功能（鉴权、计费、SLA等）

---

## 核心工具功能对比

### ✅ 完整实现的功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 搜索聚合 | ✅ 完整 | SearXNG + 可选 Brave |
| 内容提取 | ✅ 完整 | Reader/HTTP/浏览器多级提取 |
| 结果缓存 | ✅ 完整 | Redis + SQLite 本地索引 |
| 异步重试 | ✅ 完整 | Backfill worker + 指数退避 |
| 域名过滤 | ✅ 完整 | include/exclude_domains |
| 内容清洗 | ✅ 完整 | trafilatura + Reader |
| 质量评分 | ✅ 完整 | 关键词匹配 + 长度评分 |
| 文本分块 | ✅ 完整 | chunks_per_source |
| 抽取式答案 | ✅ 完整 | 高分句子拼接 |

---

## 🔴 高优先级功能缺失

### 1. 时间范围过滤 ⭐⭐⭐⭐⭐

**当前：** 无法限制搜索结果的发布时间  
**需求：** `days=7` 只返回最近 7 天的内容

**影响场景：**
- 新闻搜索必须要时效性
- 技术文档需要最新版本
- 事件追踪需要时间线

**SearXNG 原生支持：** ✅ 是的！

```python
# SearXNG API 参数
{
    "time_range": "day",    # 最近 1 天
    "time_range": "week",   # 最近 1 周  
    "time_range": "month",  # 最近 1 月
    "time_range": "year"    # 最近 1 年
}

# 实现方案
class TavilySearchRequest(BaseModel):
    days: Optional[int] = None  # 1, 7, 30, 365

def map_days_to_time_range(days: Optional[int]) -> Optional[str]:
    if days is None:
        return None
    if days <= 1:
        return "day"
    elif days <= 7:
        return "week"
    elif days <= 30:
        return "month"
    elif days <= 365:
        return "year"
    return None

# 在 search_providers.py 的 SearXNG 提供者中
class SearXNGProvider:
    async def search(self, request: SearchProviderRequest):
        params = {
            "q": request.query,
            "format": "json",
            "time_range": map_days_to_time_range(request.days)  # 新增
        }
```

**实现难度：** 🟢 非常低（半天）  
**优先级：** 🔴 极高  
**可行性：** ✅ SearXNG 原生支持，直接传参即可

---

### 2. 主题/类别感知搜索 ⭐⭐⭐⭐

**当前：** 统一搜索策略，不区分内容类型  
**需求：** 根据内容类型优化搜索引擎选择和排序

**影响场景：**
- 新闻搜索：时效性 > 权威性 > 全面性
- 学术搜索：权威性 > 全面性 > 时效性
- 代码搜索：准确性 > 权威性

**SearXNG 引擎支持：** ✅ 是的！

```python
# SearXNG 可以指定搜索引擎类别
# 参考：https://docs.searxng.org/user/configured_engines.html

TOPIC_ENGINE_CONFIG = {
    "general": {
        "engines": "google,bing,duckduckgo",  # 通用搜索引擎
        "time_range": None,
    },
    "news": {
        "engines": "google news,bing news,reddit",  # 新闻引擎
        "time_range": "week",  # 默认最近一周
        "categories": "news"
    },
    "academic": {
        "engines": "google scholar,arxiv,semantic scholar",  # 学术引擎
        "categories": "science",
        "prefer_domains": [".edu", ".org", "arxiv.org", "scholar.google.com"]
    },
    "code": {
        "engines": "github,stackoverflow,gitlab",  # 代码引擎
        "categories": "it",
        "prefer_domains": ["github.com", "stackoverflow.com", "gitlab.com"]
    },
    "images": {
        "engines": "google images,bing images",
        "categories": "images"
    }
}

# 实现
class TavilySearchRequest(BaseModel):
    topic: Literal["general", "news", "academic", "code", "images"] = "general"

class SearXNGProvider:
    async def search(self, request: SearchProviderRequest):
        config = TOPIC_ENGINE_CONFIG.get(request.topic, TOPIC_ENGINE_CONFIG["general"])
        
        params = {
            "q": request.query,
            "format": "json",
            "engines": config["engines"],  # 指定引擎
            "categories": config.get("categories"),  # 指定类别
            "time_range": config.get("time_range") or map_days_to_time_range(request.days)
        }
        
        response = await self.client.get(self.base_url, params=params)
        results = response.json()["results"]
        
        # 后处理：域名偏好过滤
        if "prefer_domains" in config:
            results = self._prioritize_domains(results, config["prefer_domains"])
        
        return results
    
    def _prioritize_domains(self, results, preferred_domains):
        # 将偏好域名的结果提升排序
        preferred = []
        others = []
        for r in results:
            domain = urlparse(r["url"]).netloc
            if any(domain.endswith(d) for d in preferred_domains):
                preferred.append(r)
            else:
                others.append(r)
        return preferred + others
```

**实现难度：** 🟢 低（1-2 天）  
**优先级：** 🔴 高  
**可行性：** ✅ SearXNG 原生支持引擎和类别筛选

---

### 3. 内容语义去重 ⭐⭐⭐⭐

**当前：** 仅基于 URL 去重，无法识别相同内容的不同 URL  
**问题：** 
- 同一篇文章在多个站点转载
- 相似但稍有差异的内容都会返回
- 浪费提取资源和返回结果空间

**需求：** 检测并去除语义相似的重复内容

**实现方案：**
```python
# 方案 1: 轻量级文本指纹（SimHash）- 推荐
from simhash import Simhash

def deduplicate_by_simhash(results, threshold=3):
    """
    使用 SimHash 去重，threshold 为汉明距离阈值
    threshold=3 表示允许 3 位差异（约 94% 相似度）
    """
    unique = []
    seen_hashes = []
    
    for result in results:
        content = result.get('content', '')
        if len(content) < 100:  # 内容太短，跳过去重
            unique.append(result)
            continue
        
        hash_val = Simhash(content).value
        
        # 计算与已见哈希的汉明距离
        is_duplicate = False
        for seen_hash in seen_hashes:
            hamming_distance = bin(hash_val ^ seen_hash).count('1')
            if hamming_distance <= threshold:
                is_duplicate = True
                break
        
        if not is_duplicate:
            unique.append(result)
            seen_hashes.append(hash_val)
    
    return unique

# 方案 2: 向量相似度（可选，需要 embedding 模型）
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

class SemanticDeduplicator:
    def __init__(self):
        # 使用轻量级模型
        self.model = SentenceTransformer('all-MiniLM-L6-v2')  # 80MB
    
    def deduplicate(self, results, threshold=0.85):
        if len(results) <= 1:
            return results
        
        # 提取文本摘要（前500字符）
        texts = [r.get('content', '')[:500] for r in results]
        embeddings = self.model.encode(texts)
        
        # 计算相似度矩阵
        similarities = cosine_similarity(embeddings)
        
        # 贪心去重
        unique_indices = [0]  # 保留第一个
        for i in range(1, len(results)):
            # 检查与已选结果的相似度
            max_sim = max(similarities[i][j] for j in unique_indices)
            if max_sim < threshold:
                unique_indices.append(i)
        
        return [results[i] for i in unique_indices]

# 集成到 main.py
async def search(request: SearchRequest):
    # ... 现有逻辑 ...
    
    # 在返回前去重
    if request.semantic_deduplication:  # 新增开关
        crawl_result["results"] = deduplicate_by_simhash(
            crawl_result["results"],
            threshold=3
        )
```

**实现难度：** 🟡 中（SimHash 2天 / 向量 3-4天）  
**优先级：** 🔴 高  
**推荐方案：** SimHash（轻量、快速、无需 GPU）

---

### 4. 深度研究模式（多轮搜索） ⭐⭐⭐⭐

**当前：** 单轮搜索  
**Tavily：** 从初始结果中提取相关查询，进行多轮搜索

**需求：** 
- 自动扩展相关主题
- 从初始结果中发现新查询词
- 更全面的内容覆盖

**实现方案：**
```python
# 新增端点：POST /tavily/research
class ResearchRequest(BaseModel):
    query: str
    max_iterations: int = Field(default=3, ge=1, le=5)
    max_results_per_iteration: int = Field(default=10, ge=5, le=20)
    max_total_results: int = Field(default=20, ge=10, le=50)

class ResearchMode:
    """深度研究模式：多轮迭代搜索"""
    
    def extract_related_queries(self, results: list[dict], original_query: str) -> list[str]:
        """从搜索结果中提取相关查询词"""
        related_queries = set()
        query_terms = set(tokenize(original_query))
        
        for result in results[:5]:  # 只分析前5个结果
            content = result.get('content', '')
            title = result.get('title', '')
            
            # 方法 1: 提取标题中的关键短语
            title_phrases = self._extract_noun_phrases(title)
            related_queries.update(title_phrases[:3])
            
            # 方法 2: 提取高频共现词组
            cooccurring = self._find_cooccurring_terms(content, query_terms)
            related_queries.update(cooccurring[:2])
            
            # 方法 3: 提取引用和链接锚文本
            if 'raw_content' in result:
                quoted_text = self._extract_quoted_text(result['raw_content'])
                related_queries.update(quoted_text[:2])
        
        # 过滤：去除太短或太长的查询
        filtered = [
            q for q in related_queries 
            if 3 <= len(q.split()) <= 6 and q.lower() != original_query.lower()
        ]
        
        return filtered[:5]  # 最多5个相关查询
    
    def _extract_noun_phrases(self, text: str) -> list[str]:
        """提取名词短语（简化版，无需NLP库）"""
        # 简单实现：提取引号内容、标题case的词组
        phrases = []
        
        # 引号内容
        import re
        quoted = re.findall(r'"([^"]+)"', text)
        phrases.extend(quoted)
        
        # 大写开头的连续词组
        title_case = re.findall(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', text)
        phrases.extend(title_case)
        
        return phrases
    
    def _find_cooccurring_terms(self, text: str, query_terms: set[str], window=50) -> list[str]:
        """查找与查询词共现的术语"""
        words = tokenize(text)
        cooccurring = []
        
        for i, word in enumerate(words):
            if word.lower() in query_terms:
                # 提取窗口内的词
                start = max(0, i - window)
                end = min(len(words), i + window)
                context = words[start:end]
                
                # 提取2-3词短语
                for j in range(len(context) - 1):
                    phrase = ' '.join(context[j:j+3])
                    if len(phrase.split()) >= 2:
                        cooccurring.append(phrase)
        
        # 返回高频短语
        from collections import Counter
        return [phrase for phrase, count in Counter(cooccurring).most_common(5)]
    
    def _extract_quoted_text(self, html: str) -> list[str]:
        """提取HTML中的引用文本"""
        soup = BeautifulSoup(html, 'html.parser')
        quotes = []
        
        for tag in soup.find_all(['blockquote', 'q']):
            text = tag.get_text(strip=True)
            if 10 <= len(text) <= 100:
                quotes.append(text)
        
        return quotes[:5]
    
    async def research(self, request: ResearchRequest) -> dict:
        """执行深度研究"""
        all_results = []
        seen_urls = set()
        queries_to_try = [request.query]
        tried_queries = set()
        
        for iteration in range(request.max_iterations):
            logger.info(f"Research iteration {iteration + 1}/{request.max_iterations}")
            
            # 本轮要尝试的查询
            current_queries = [
                q for q in queries_to_try 
                if q not in tried_queries
            ][:3]  # 每轮最多3个查询
            
            if not current_queries:
                logger.info("No more queries to try, stopping research")
                break
            
            # 执行搜索
            for query in current_queries:
                tried_queries.add(query)
                
                # 调用现有搜索
                search_result = await search(SearchRequest(
                    query=query,
                    limit=request.max_results_per_iteration,
                    mode="crawl"
                ))
                
                # 去重并添加结果
                for result in search_result.get('results', []):
                    url = result.get('reference', '')
                    if url and url not in seen_urls:
                        result['source_query'] = query
                        result['iteration'] = iteration + 1
                        all_results.append(result)
                        seen_urls.add(url)
                
                # 提取新的相关查询
                if iteration < request.max_iterations - 1:
                    new_queries = self.extract_related_queries(
                        search_result.get('results', []),
                        request.query
                    )
                    queries_to_try.extend(new_queries)
            
            # 检查是否已达到结果上限
            if len(all_results) >= request.max_total_results:
                logger.info(f"Reached max results limit: {len(all_results)}")
                break
        
        # 去重和排序
        all_results = deduplicate_by_simhash(all_results)
        all_results = sorted(
            all_results,
            key=lambda r: r.get('quality_score', 0),
            reverse=True
        )[:request.max_total_results]
        
        return {
            "query": request.query,
            "mode": "research",
            "iterations": iteration + 1,
            "queries_tried": list(tried_queries),
            "results": all_results,
            "total_results": len(all_results),
        }

# 在 main.py 中添加端点
@app.post("/tavily/research")
async def deep_research(request: ResearchRequest):
    """深度研究模式：多轮迭代搜索"""
    research_mode = ResearchMode()
    return await research_mode.research(request)
```

**实现难度：** 🟡 中高（4-5 天）  
**优先级：** 🟡 中（核心场景够用，高级用户需要）  
**挑战：** 查询扩展质量决定效果，需要迭代优化

---

## 🟡 中优先级功能

### 5. 图片/媒体提取 ⭐⭐⭐

**当前：** 仅返回文本内容  
**需求：** 提取页面相关图片

**实现方案：**
```python
class TavilySearchRequest(BaseModel):
    include_images: bool = False
    max_images_per_source: int = Field(default=3, ge=1, le=10)

def extract_images_from_html(html: str, base_url: str, max_images: int = 3) -> list[dict]:
    """提取页面主要图片"""
    soup = BeautifulSoup(html, 'html.parser')
    images = []
    
    # 优先级1: Open Graph 图片
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        images.append({
            "url": urljoin(base_url, og_image['content']),
            "alt": "Featured image",
            "type": "og:image",
            "priority": 1
        })
    
    # 优先级2: 文章/内容区域的图片
    content_selectors = ['article img', '.content img', 'main img', '.post img']
    for selector in content_selectors:
        for img in soup.select(selector):
            if img.get('src') and not _is_icon_or_ad(img):
                images.append({
                    "url": urljoin(base_url, img['src']),
                    "alt": img.get('alt', ''),
                    "type": "content",
                    "priority": 2
                })
    
    # 优先级3: 其他图片
    for img in soup.find_all('img'):
        if img.get('src') and not _is_icon_or_ad(img):
            images.append({
                "url": urljoin(base_url, img['src']),
                "alt": img.get('alt', ''),
                "type": "general",
                "priority": 3
            })
    
    # 去重并排序
    seen = set()
    unique = []
    for img in images:
        if img['url'] not in seen:
            seen.add(img['url'])
            unique.append(img)
    
    # 按优先级排序
    unique.sort(key=lambda x: x['priority'])
    return unique[:max_images]

def _is_icon_or_ad(img) -> bool:
    """过滤图标、logo、广告"""
    src = img.get('src', '').lower()
    alt = img.get('alt', '').lower()
    
    # 检查常见图标/广告关键词
    exclude_keywords = ['icon', 'logo', 'avatar', 'emoji', 'ad', 'banner', 'pixel', 'tracking']
    if any(kw in src or kw in alt for kw in exclude_keywords):
        return True
    
    # 检查尺寸
    try:
        width = int(img.get('width', 0))
        height = int(img.get('height', 0))
        if width and height and (width < 100 or height < 100):
            return True
    except:
        pass
    
    return False
```

**实现难度：** 🟢 低（2 天）  
**优先级：** 🟡 中

---

## 🟢 低优先级增强

### 6. 智能内容摘要

使用抽取式摘要而非截断：

```python
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lex_rank import LexRankSummarizer

def extractive_summary(text: str, sentences: int = 3) -> str:
    try:
        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summarizer = LexRankSummarizer()
        summary = summarizer(parser.document, sentences)
        return " ".join(str(sentence) for sentence in summary)
    except:
        return text[:500]  # 回退到截断
```

---

## 总结：工具功能优化路线

### 🚀 Phase 1: 核心增强（1 周）

**立即可做，SearXNG 原生支持：**

1. ✅ **时间过滤** - 0.5 天
   - 直接使用 SearXNG 的 `time_range` 参数
   - 新增 `days` 参数映射

2. ✅ **主题分类搜索** - 1-2 天
   - 利用 SearXNG 的引擎和类别筛选
   - 配置不同主题的引擎组合

3. ✅ **轻量级去重** - 2 天
   - SimHash 算法（无需额外依赖）
   - 集成到返回结果处理流程

**效果：** 新闻搜索、学术搜索等场景体验提升 50%+

---

### 🎯 Phase 2: 高级功能（2 周）

4. ✅ **深度研究模式** - 4-5 天
   - 多轮查询扩展
   - 相关查询提取
   - 结果合并和去重

5. ✅ **图片提取** - 2 天
   - 基础图片提取和过滤
   - Open Graph 支持

**效果：** 深度研究场景可用，多媒体支持

---

### 🏆 Phase 3: 锦上添花（按需）

6. 智能摘要（抽取式）
7. 结果多样性优化
8. 结构化数据提取

---

## 与 Tavily 的使用体验对比

| 场景 | 当前 TrailSearch | + Phase 1 | + Phase 2 |
|------|---------------|-----------|-----------|
| **通用搜索** | ⭐⭐⭐⭐ 85% | ⭐⭐⭐⭐⭐ 95% | ⭐⭐⭐⭐⭐ 95% |
| **新闻搜索** | ⭐⭐⭐ 65% | ⭐⭐⭐⭐⭐ 95% | ⭐⭐⭐⭐⭐ 95% |
| **学术搜索** | ⭐⭐⭐ 70% | ⭐⭐⭐⭐ 90% | ⭐⭐⭐⭐⭐ 95% |
| **深度研究** | ⭐⭐⭐ 65% | ⭐⭐⭐⭐ 80% | ⭐⭐⭐⭐⭐ 95% |
| **多媒体** | ⭐⭐ 40% | ⭐⭐ 40% | ⭐⭐⭐⭐ 85% |

**最终结论：** 

1. **Phase 1 优先级最高**：时间过滤和主题分类都是 SearXNG 原生支持，实现成本极低，效果显著
2. **Phase 2 提供完整体验**：深度研究模式是高级用户的核心需求，图片支持补齐多媒体短板
3. **总体评估**：完成 Phase 1+2 后，TrailSearch 在工具属性上可达到 Tavily 90-95% 的使用体验

**推荐策略：** 先快速完成 Phase 1（1周），立即提升核心场景体验；再根据实际使用反馈决定是否投入 Phase 2。
