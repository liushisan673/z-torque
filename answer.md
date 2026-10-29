一、API简介​
从全网搜索任何网页信息和网页链接，结果准确、摘要完整，更适合AI使用。​
​
🏷️
可配置搜索时间范围、是否显示摘要，支持按分页获取更多结果。​
​
二、搜索结果​
包括网页、图片、视频，Response格式兼容Bing Search API。​
•
网页包括name、url、snippet、summary、siteName、siteIcon、datePublished等信息​
​

​
•
图片包括 contentUrl、hostPageUrl、width、height等信息​
•
视频搜索目前在WebSearch中暂未开放​
三、API接口​
​
接口域名：https://api.bocha.cn/​
EndPoint：https://api.bocha.cn/v1/web-search​
​
四、调用方式​
请求方式​
POST​
请求参数​
请求头​
​
​
参数​
​	​
取值​
​	​
说明​
​
​
Authorization​
​	​
Bearer {API KEY} ​
​
​	​
鉴权参数，示例：Bearer xxxxxx，API KEY请先前往博查AI开放平台（https://open.bocha.cn）> API KEY 管理中获取。​
​
​
Content-Type ​
​	​
application/json ​
​	​
解释请求正文的方式。 ​
​
​
请求体​
​
​
参数​
​	​
类型​
​	​
必填​
​	​
说明​
​
​
query​
​	​
String ​
​	​
是​
​	​
用户的搜索词。​
​
​
freshness​
​	​
String​
​
​	​
否​
​	​
搜索指定时间范围内的网页。​
可填值：​
•
noLimit，不限（默认）​
•
oneDay，一天内​
•
oneWeek，一周内​
•
oneMonth，一个月内​
•
oneYear，一年内​
•
YYYY-MM-DD..YYYY-MM-DD，搜索日期范围，例如："2025-01-01..2025-04-06"​
•
YYYY-MM-DD，搜索指定日期，例如："2025-04-06"​
​
👻
推荐使用“noLimit”。搜索算法会自动进行时间范围的改写，效果更佳。如果指定时间范围，很有可能出现时间范围内没有相关网页的情况，导致找不到搜索结果。​
​
​
​
summary​
​
​	​
Boolean​
​	​
否​
​	​
是否显示文本摘要。​
可填值：​
•
true，显示​
•
false，不显示（默认）​
​
​
include​
​	​
String​
​	​
否​
​	​
指定搜索的网站范围。多个域名使用|或,分隔，最多不能超过100个​
可填值：​
•
根域名​
•
子域名​
例如：qq.com|m.163.com​
​
​
exclude​
​	​
String​
​	​
否​
​	​
排除搜索的网站范围。多个域名使用|或,分隔，最多不能超过100个​
可填值：​
•
根域名​
•
子域名​
例如：qq.com|m.163.com​
​
​
count​
​
​	​
Int​
​
​	​
否​
​	​
返回结果的条数（实际返回结果数量可能会小于count指定的数量）。​
•
可填范围：1-50，最大单次搜索返回50条​
•
默认为10​
​
​