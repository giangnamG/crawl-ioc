# Thiết kế đơn giản cho hệ thống điều tra domain và IOC

## Chạy bản Flask SSR đã triển khai

Stack hiện tại:

```text
Backend/UI: Python Flask server-side rendering
Database: SQLite3
Queue MVP: bảng jobs trong SQLite
Frontend: Jinja templates + CSS thuần
```

Cài dependency:

```powershell
python -m pip install -r requirements.txt
```

Chạy server:

```powershell
python app.py
```

Mở:

```text
http://127.0.0.1:5000
```

Database mặc định:

```text
data/ioc_investigator.sqlite3
```

Ghi chú triển khai browser:

```text
ioc_app/browser.py mặc định dùng CloakBrowser thật.
Search Google và crawl target URL đều đi qua BrowserClient dùng launch_persistent_context().
HTTP fallback chỉ dùng khi set env BROWSER_PROVIDER=http để test nội bộ.
```

Cấu hình CloakBrowser qua environment:

```powershell
$env:CLOAK_HEADLESS="false"       # false khuyến nghị khi search Google
$env:CLOAK_HUMANIZE="true"
$env:CLOAK_HUMAN_PRESET="default" # dùng careful nếu bị bot mạnh hơn
$env:SEARCH_FAST_MODE="true"
$env:SEARCH_TYPE_DELAY_MIN="0"
$env:SEARCH_TYPE_DELAY_MAX="5"
$env:SEARCH_PAGE_DELAY_MIN="0"
$env:SEARCH_PAGE_DELAY_MAX="0"
$env:CLOAK_LOCALE="en-US"
$env:CLOAK_TIMEZONE="Asia/Saigon"
$env:CLOAK_PROXY="http://user:pass@host:port"  # optional
$env:CLOAK_GEOIP="true"                       # bật khi dùng proxy để match timezone/locale
$env:CLOAK_FINGERPRINT_SEED="12345"           # optional, giữ fingerprint ổn định
$env:SEARCH_MAX_PAGES="0"        # 0 = chạy đến khi Google không còn result mới
```

Binary CloakBrowser:

```powershell
python -m cloakbrowser install
python -m cloakbrowser info
```

Nếu Google đã trả CAPTCHA/anti-bot:

```powershell
# dừng app trước, sau đó reset profile Google local
Remove-Item -Recurse -Force .\data\cloak_profiles\google

# chạy lại với headed mode + humanize + proxy residential nếu IP hiện tại bị đánh dấu
$env:CLOAK_HEADLESS="false"
$env:CLOAK_HUMANIZE="true"
$env:CLOAK_HUMAN_PRESET="careful"
$env:CLOAK_PROXY="http://user:pass@residential-proxy:port"
$env:CLOAK_GEOIP="true"
python .\app.py
```

CloakBrowser giúp browser fingerprint giống trình duyệt thật hơn, nhưng không phải CAPTCHA solver. Với Google Search, IP/VPN/datacenter proxy kém uy tín vẫn có thể bị block dù đang dùng CloakBrowser.

Các màn chính:

- `Dashboard`: tổng quan số keyword, query, URL, domain, IOC, job.
- `Keywords`: import keyword và chọn Google dork template.
- `Keywords`: có thể chọn `Full Google Query` để queue nguyên dòng như `site:88i.*`, không kết hợp thêm dork.
- `Search Dorks`: tạo/preview Google dork template.
- `Review`: approve/reject URL/domain.
- `Rules`: tạo/test regex rule extract IOC.
- `Crawl`: theo dõi URL approved/crawled.
- `Jobs`: chạy job thủ công cho MVP.
- `Jobs`: khi chạy bằng `python app.py`, background worker tự xử lý job pending; nút thủ công vẫn giữ để kiểm soát.
- `IOCs`: xem IOC duy nhất và nguồn IOC.

## Mục tiêu

Tự động hóa một luồng đơn giản:

1. Nhập danh sách keyword.
2. User cấu hình Google dork template trên giao diện.
3. Backend kết hợp keyword với Google dork template để tạo search query cụ thể.
4. Dùng CloakBrowser search Google theo từng search query.
5. Lưu URL/title kết quả tìm kiếm.
6. User review và loại URL/domain không hợp lệ.
7. Crawl các URL còn lại.
8. Trích xuất IOC: domain, URL, email, phone, hash, địa chỉ.
9. URL/domain mới phát hiện được đưa lại vào danh sách review.

Nguyên tắc quan trọng:

- URL đã crawl rồi thì không crawl lại.
- IOC không được trùng.
- Một IOC có thể có nhiều nguồn phát hiện.
- Domain phải biết được nguồn phát hiện: từ Google Search hay từ domain/URL khác.

## Flow cụ thể theo yêu cầu

```mermaid
flowchart TD
  A["User nhập danh sách keyword"] --> A2["User chọn/tạo Google dork template"]
  A2 --> B["Backend render keyword + dork thành search query"]
  B --> C["Tạo job: search_query"]
  C --> D["Worker lấy từng search query trong queue"]
  D --> E["CloakBrowser mở Google và search full query"]
  E --> F["Lấy tất cả URL/title/snippet/rank qua từng page Google"]
  F --> G["Lưu vào DB và hiển thị lên Web UI"]
  F --> G2["Đánh dấu domain source = google_search"]
  G --> H["User review kết quả"]
  H --> I["Reject domain/URL không hợp lệ"]
  H --> J["Approve domain/URL hợp lệ"]
  J --> K["Tạo job: crawl_url"]
  K --> L["CloakBrowser truy cập từng URL chưa crawl"]
  L --> M["IOC Extractor tìm hash/email/phone/domain/url/address"]
  M --> N["Lưu IOC duy nhất và nguồn IOC"]
  M --> O["Domain/URL mới quay lại danh sách review"]
  M --> O2["Đánh dấu domain source = extracted_from_crawl"]
  O --> H
```

Luồng này cố ý chỉ giữ 4 thành phần chính:

- `Web UI`: nhập keyword, xem kết quả, approve/reject URL.
- `Backend App`: API + xử lý nghiệp vụ.
- `Database`: lưu keyword, URL, crawl status, IOC, nguồn IOC.
- `Background Worker`: chạy search, crawl và extract.

Không cần Redis, S3/MinIO, nhiều worker riêng, nhiều search provider ở bản đầu.

## Luồng nghiệp vụ chi tiết

### Bước 1: Frontend nhập keyword và Google dork template

User nhập danh sách keyword từ giao diện.

Ví dụ:

```text
88i
88i casino
88i betting
site 88i
```

User cũng có thể tạo hoặc chọn Google dork template trên giao diện.

Ví dụ dork template:

```text
{keyword}
"{keyword}"
site:{keyword}
inurl:{keyword}
intitle:"{keyword}"
"{keyword}" casino
"{keyword}" betting
"{keyword}" "contact"
"{keyword}" "telegram"
"{keyword}" "hotline"
```

Trong đó `{keyword}` là placeholder bắt buộc. Backend sẽ thay `{keyword}` bằng keyword thực tế.

Ví dụ keyword:

```text
88i
```

Kết quả search query được tạo:

```text
88i
"88i"
site:88i
inurl:88i
intitle:"88i"
"88i" casino
"88i" betting
"88i" "contact"
```

Khi user bấm `Import`, backend làm 5 việc:

1. Tách từng dòng thành một keyword.
2. Lưu keyword vào bảng `keywords`.
3. Lấy danh sách dork template user đã chọn.
4. Render từng cặp `keyword + dork template` thành `search_query`.
5. Tạo một job `search_query` cho từng search query trong bảng `jobs`.

Job được tạo:

```text
type = search_query
status = pending
payload = { search_query_id }
```

Kết quả sau bước này:

```text
keywords:
- 88i
- 88i casino
- 88i betting

search_dorks:
- {keyword}
- "{keyword}"
- inurl:{keyword}
- "{keyword}" "contact"

search_queries:
- keyword = 88i, dork = {keyword}, query = 88i
- keyword = 88i, dork = "{keyword}", query = "88i"
- keyword = 88i, dork = inurl:{keyword}, query = inurl:88i
- keyword = 88i, dork = "{keyword}" "contact", query = "88i" "contact"

jobs:
- search_query cho 88i
- search_query cho "88i"
- search_query cho inurl:88i
- search_query cho "88i" "contact"
```

Worker sẽ xử lý từng search query một. Nếu muốn đơn giản và ổn định, MVP chỉ chạy 1 worker search tại một thời điểm.

Lưu ý: keyword không được đưa trực tiếp vào Google nếu user không chọn template `{keyword}`. Backend luôn search bằng `search_queries.query_text`, tức là cụm Google dork đã render.

### Bước 2: CloakBrowser search từng Google dork query

Worker lấy job `search_query`.

Với mỗi search query:

1. Mở browser bằng CloakBrowser.
2. Truy cập `https://www.google.com`.
3. Nhập full query đã render vào ô search.
4. Chờ trang kết quả load.
5. Parse danh sách kết quả trên trang hiện tại.
6. Lấy các trường:
   - `url`
   - `title`
   - `snippet`
   - `rank`
   - `page_no`
   - `keyword_id`
   - `search_query_id`
   - `dork_id`
   - `query_text`
7. Bấm nút `Next` hoặc điều hướng sang trang kết quả tiếp theo.
8. Lặp lại cho đến khi:
   - Không còn trang tiếp theo.
   - Google chặn/CAPTCHA.
   - Quá timeout cấu hình.
   - Gặp page trùng với page đã parse trước đó.

Mỗi kết quả search được chuẩn hóa URL rồi lưu vào bảng `urls`.

Để vẫn giữ flow đơn giản:

- `urls` lưu URL duy nhất theo `url_norm`.
- `url_sources` lưu URL đó được tìm thấy từ keyword nào, page nào, rank nào.
- `search_queries` lưu cụm Google dork thực tế đã dùng để search.
- `domains` lưu domain duy nhất.
- `domain_sources` lưu domain đó được phát hiện từ Google Search hay từ domain/URL khác.

Như vậy nếu cùng một URL xuất hiện ở nhiều keyword khác nhau, hệ thống vẫn biết đầy đủ nguồn tìm thấy URL đó.
Nếu cùng một domain vừa xuất hiện từ Google vừa được phát hiện từ một domain khác, hệ thống vẫn hiển thị được cả hai nguồn.

Kết quả sau bước này:

```text
urls:
- https://a.com
- https://b.com
- https://c.com

url_sources:
- https://a.com xuất hiện từ query "88i", page 1, rank 1
- https://a.com xuất hiện từ query "88i casino", page 2, rank 14
- https://b.com xuất hiện từ query "inurl:88i", page 1, rank 2

domains:
- a.com
- b.com

domain_sources:
- a.com được collect từ google_search, query "88i"
- a.com được collect từ google_search, query "88i casino"
- b.com được collect từ google_search, query "inurl:88i"
```

Kết quả hiển thị ngay trên UI.

### Bước 3: Hiển thị kết quả để user loại domain/URL không hợp lệ

Frontend hiển thị danh sách kết quả search.

Bảng review nên có các cột tối thiểu:

- Checkbox chọn dòng.
- Domain.
- Domain source: `google_search`, `extracted_from_crawl`, hoặc cả hai.
- URL.
- Title.
- Keyword nguồn.
- Google dork/query nguồn.
- Page/rank Google.
- Review status.
- Crawl status.

User có thể lọc theo:

- Keyword.
- Domain.
- Pending review.
- Approved.
- Rejected.

User thao tác:

- `Approve URL`: URL được phép crawl.
- `Reject URL`: URL bị loại.
- `Reject domain`: toàn bộ URL thuộc domain đó bị loại.

Theo yêu cầu "xóa domain không hợp lệ khỏi kết quả", UI có thể ẩn các dòng rejected khỏi danh sách mặc định. Trong DB vẫn nên giữ lại bằng `review_status = rejected` để audit và tránh import/search lại làm hiện lại domain đó.

Khi user reject domain:

```text
Input:
- domain = spam-example.com

Backend:
- update domains set review_status = rejected where domain = spam-example.com
- update urls set review_status = rejected where domain = spam-example.com
- các URL thuộc domain này không được tạo crawl job
```

Khi user approve URL:

```text
Input:
- url_id = 123

Backend:
- update urls set review_status = approved where id = 123
- nếu domain của URL chưa rejected thì giữ/đặt domain review_status = approved
- nếu URL chưa crawl và chưa có crawl job pending/running thì tạo job crawl_url
```

Job được tạo:

```text
type = crawl_url
status = pending
payload = { url_id }
```

### Bước 4: CloakBrowser truy cập từng URL/domain đã approve

Worker lấy job `crawl_url`.

Trước khi truy cập, worker kiểm tra:

```text
Nếu review_status != approved:
  bỏ qua

Nếu crawl_status = crawled:
  bỏ qua

Nếu URL đã có job crawl_url pending/running:
  không tạo job mới
```

Quy tắc đúng theo yêu cầu:

- URL đã truy cập rồi thì không truy cập lại.
- Hai URL chung domain vẫn là hai URL riêng.
- Ví dụ `https://a.com` và `https://a.com/promo` là 2 target khác nhau.
- Nếu cả hai đều chưa crawl và đều được approve thì vẫn crawl cả hai.

Khi crawl:

- Mở URL bằng CloakBrowser.
- Lấy final URL sau redirect.
- Lấy HTML/rendered text.
- Có thể lấy thêm toàn bộ link trong DOM: `a[href]`, `script[src]`, `iframe[src]`, `form[action]`, `img[src]`.
- Lưu `status_code`, `final_url`, `html`, `crawled_at`, `crawl_status`.
- Chuyển nội dung cho IOC Extractor.

Kết quả sau bước này:

```text
urls:
- url_norm = https://a.com
  review_status = approved
  crawl_status = crawled
  final_url = https://a.com/home
  status_code = 200
```

### Bước 5: Extract IOC từ từng URL đã crawl

IOC Extractor tìm:

- Email.
- Số điện thoại.
- URL khác.
- Domain khác.
- MD5/SHA1/SHA256/SHA512.
- Địa chỉ.

Nguồn dữ liệu đưa vào extractor:

- HTML.
- Rendered text.
- Link trong DOM.
- Final URL.
- Redirect URL nếu có.

Mỗi IOC được chuẩn hóa rồi lưu vào bảng `iocs`.

Nếu IOC đã tồn tại, không tạo dòng mới.

Nguồn phát hiện lưu riêng trong bảng `ioc_sources`.

Ví dụ:

```text
iocs:
- email: support@example.com

ioc_sources:
- support@example.com được thấy ở https://site-a.com/contact
- support@example.com được thấy ở https://site-b.com/help
```

Điều này giải quyết yêu cầu:

```text
Mỗi IOC là duy nhất.
Nếu nhiều nguồn có chung 1 IOC, vẫn xem được IOC đó lấy từ nguồn nào.
```

### Bước 6: URL/domain mới được nạp lại vào danh sách review

Nếu extractor thấy URL/domain mới:

- Chuẩn hóa URL/domain.
- Nếu chưa tồn tại trong bảng `urls`, tạo mới với `first_source = extracted_from_crawl`.
- Nếu domain chưa tồn tại trong bảng `domains`, tạo mới với `first_source = extracted_from_crawl`.
- Luôn ghi nguồn vào `domain_sources`, ví dụ domain này được phát hiện khi crawl URL nào.
- Đặt trạng thái `pending_review`.
- Không tự crawl ngay.

User vẫn là người quyết định URL/domain mới có hợp lệ hay không.

Ví dụ:

```text
Đang crawl:
- https://a.com

Extractor thấy:
- https://payment-a.net
- support@payment-a.net
- 0988123456

Backend lưu:
- IOC email support@payment-a.net
- IOC phone 0988123456
- URL https://payment-a.net với review_status = pending_review
- Domain payment-a.net với source = extracted_from_crawl, source_url = https://a.com
```

Sau đó `https://payment-a.net` xuất hiện lại ở màn hình review. Nếu user approve, hệ thống tạo job `crawl_url` mới và tiếp tục vòng lặp.

## Tóm tắt vòng lặp chính

```text
Keyword
  -> Google Search
  -> URL/domain result
  -> User review
  -> Crawl approved URL
  -> Extract IOC + URL/domain mới
  -> URL/domain mới quay lại User review
  -> tiếp tục crawl nếu user approve
```

Vòng lặp chỉ dừng khi không còn URL/domain mới nào được approve để crawl.

## Data model đơn giản

### `keywords`

```sql
CREATE TABLE keywords (
  id BIGSERIAL PRIMARY KEY,
  text TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### `search_dorks`

Bảng này lưu Google dork template do user định nghĩa trên giao diện.

```sql
CREATE TABLE search_dorks (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  template TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT true,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Ví dụ:

```text
name = Exact keyword
template = "{keyword}"

name = In URL
template = inurl:{keyword}

name = Contact page
template = "{keyword}" "contact"
```

### `search_queries`

Bảng này lưu query thực tế đã render từ `keyword + search_dork`.

```sql
CREATE TABLE search_queries (
  id BIGSERIAL PRIMARY KEY,
  keyword_id BIGINT NOT NULL REFERENCES keywords(id),
  dork_id BIGINT NOT NULL REFERENCES search_dorks(id),
  query_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (keyword_id, dork_id, query_text)
);
```

Ví dụ:

```text
keyword = 88i
template = "{keyword}" "contact"
query_text = "88i" "contact"
```

### `jobs`

Bảng này thay Redis Queue trong bản MVP.

```sql
CREATE TABLE jobs (
  id BIGSERIAL PRIMARY KEY,
  type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  payload JSONB NOT NULL,
  attempts INT NOT NULL DEFAULT 0,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);
```

Job status:

```text
pending
running
done
failed
skipped
```

### `urls`

Một bảng duy nhất cho URL đã chuẩn hóa. Mỗi URL chỉ có một dòng.

```sql
CREATE TABLE urls (
  id BIGSERIAL PRIMARY KEY,
  url_raw TEXT NOT NULL,
  url_norm TEXT NOT NULL UNIQUE,
  domain TEXT NOT NULL,
  first_source TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending_review',
  crawl_status TEXT NOT NULL DEFAULT 'not_crawled',
  final_url TEXT,
  status_code INT,
  html TEXT,
  crawled_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### `domains`

Một bảng duy nhất cho domain đã chuẩn hóa. Domain source được lưu riêng để biết domain được collect từ Google hay từ domain khác.

```sql
CREATE TABLE domains (
  id BIGSERIAL PRIMARY KEY,
  domain TEXT NOT NULL UNIQUE,
  first_source TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending_review',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Review status của domain:

```text
pending_review
approved
rejected
```

### `domain_sources`

Bảng này lưu nguồn phát hiện domain.

Một domain có thể có nhiều nguồn. Ví dụ `example.com` vừa được thấy trong Google Search, vừa được phát hiện khi crawl `another-domain.com`.

```sql
CREATE TABLE domain_sources (
  id BIGSERIAL PRIMARY KEY,
  domain_id BIGINT NOT NULL REFERENCES domains(id),
  source_type TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  keyword_id BIGINT REFERENCES keywords(id),
  search_query_id BIGINT REFERENCES search_queries(id),
  source_url_id BIGINT REFERENCES urls(id),
  discovered_url_id BIGINT REFERENCES urls(id),
  rank INT,
  page_no INT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Source type:

```text
google_search
extracted_from_crawl
manual
```

Ý nghĩa:

- `google_search`: domain xuất hiện trong kết quả Google.
- `extracted_from_crawl`: domain được tìm thấy khi crawl một URL/domain khác.
- `manual`: user thêm thủ công.

UI không nên chỉ đọc `domains.first_source`. Để hiển thị đầy đủ, UI nên aggregate từ `domain_sources`.

Ví dụ:

```text
domain = payment-a.net
sources = ["google_search", "extracted_from_crawl"]
display = Google Search + From Domain
```

### `url_sources`

Bảng này lưu URL được phát hiện từ đâu.

Cần bảng này vì một URL có thể xuất hiện từ nhiều keyword hoặc từ nhiều trang crawl khác nhau.

```sql
CREATE TABLE url_sources (
  id BIGSERIAL PRIMARY KEY,
  url_id BIGINT NOT NULL REFERENCES urls(id),
  source_type TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  keyword_id BIGINT REFERENCES keywords(id),
  search_query_id BIGINT REFERENCES search_queries(id),
  source_url_id BIGINT REFERENCES urls(id),
  title TEXT,
  snippet TEXT,
  rank INT,
  page_no INT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Source type:

```text
google_search
extracted_from_crawl
manual
```

Review status:

```text
pending_review
approved
rejected
```

Crawl status:

```text
not_crawled
crawling
crawled
failed
skipped
```

### `iocs`

```sql
CREATE TABLE iocs (
  id BIGSERIAL PRIMARY KEY,
  type TEXT NOT NULL,
  value_raw TEXT NOT NULL,
  value_norm TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (type, value_norm)
);
```

IOC type:

```text
domain
url
email
phone
hash_md5
hash_sha1
hash_sha256
hash_sha512
address
```

### `extraction_rules`

Bảng này lưu rule regex do user định nghĩa trên giao diện.

Backend chỉ dùng các rule có `enabled = true` để extract IOC.

```sql
CREATE TABLE extraction_rules (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  ioc_type TEXT NOT NULL,
  pattern TEXT NOT NULL,
  flags TEXT NOT NULL DEFAULT '',
  value_group INT NOT NULL DEFAULT 0,
  input_scope TEXT NOT NULL DEFAULT 'text',
  exclude_pattern TEXT,
  normalizer TEXT NOT NULL DEFAULT 'default',
  priority INT NOT NULL DEFAULT 100,
  enabled BOOLEAN NOT NULL DEFAULT true,
  description TEXT,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Ý nghĩa field:

- `name`: tên rule, ví dụ `Vietnam phone`, `Email basic`, `SHA256 hash`.
- `ioc_type`: `phone`, `email`, `url`, `domain`, `hash_md5`, `hash_sha1`, `hash_sha256`, `hash_sha512`, `address`.
- `pattern`: regex chính.
- `flags`: flag regex, ví dụ `i`, `m`, `s`.
- `value_group`: group dùng làm giá trị IOC. Nếu bằng `0`, lấy toàn bộ match.
- `input_scope`: vùng dữ liệu rule sẽ chạy.
- `exclude_pattern`: regex loại trừ match sai.
- `normalizer`: cách chuẩn hóa giá trị sau khi match.
- `priority`: rule nhỏ hơn chạy trước.
- `enabled`: bật/tắt rule.

Input scope:

```text
text
html
links
final_url
redirects
all
```

Normalizer:

```text
default
lowercase
url
domain
email
phone_vn
hash
address
none
```

Ví dụ rule:

```text
name = Vietnam mobile phone
ioc_type = phone
pattern = (?<!\d)(?:\+?84|0)(?:3|5|7|8|9)\d{8}(?!\d)
flags =
value_group = 0
input_scope = text
normalizer = phone_vn
```

```text
name = SHA256
ioc_type = hash_sha256
pattern = (?<![a-fA-F0-9])[a-fA-F0-9]{64}(?![a-fA-F0-9])
flags =
value_group = 0
input_scope = all
normalizer = hash
```

```text
name = Email basic
ioc_type = email
pattern = [a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}
flags = i
value_group = 0
input_scope = text
normalizer = email
```

### `ioc_sources`

```sql
CREATE TABLE ioc_sources (
  id BIGSERIAL PRIMARY KEY,
  ioc_id BIGINT NOT NULL REFERENCES iocs(id),
  source_url_id BIGINT NOT NULL REFERENCES urls(id),
  extraction_rule_id BIGINT REFERENCES extraction_rules(id),
  evidence_text TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (ioc_id, source_url_id, extraction_rule_id)
);
```

## Logic chống trùng

### Không crawl lại URL

Trước khi tạo job crawl:

```text
Tìm url_norm trong bảng urls.
Nếu đã có và crawl_status = crawled thì không tạo job mới.
Nếu đã có job pending/running thì không tạo job mới.
```

### IOC duy nhất

Khi phát hiện IOC:

```text
INSERT INTO iocs(type, value_raw, value_norm)
ON CONFLICT (type, value_norm) DO NOTHING
```

Sau đó luôn tạo quan hệ nguồn:

```text
ioc_sources(ioc_id, source_url_id, evidence_text)
```

### URL mới từ extractor

Khi phát hiện URL mới:

```text
Nếu url_norm chưa tồn tại:
  thêm vào urls với review_status = pending_review
Nếu đã tồn tại:
  không thêm lại

Luôn ghi thêm nguồn phát hiện vào url_sources nếu chưa có.
Luôn upsert domain vào domains.
Luôn ghi thêm nguồn phát hiện domain vào domain_sources nếu chưa có.
```

### Domain source

Khi URL/domain được lấy từ Google Search:

```text
domains.first_source = google_search nếu domain mới
domain_sources.source_type = google_search
domain_sources.keyword_id = keyword gốc
domain_sources.search_query_id = Google dork query đang search
domain_sources.discovered_url_id = URL search result
domain_sources.rank/page_no = vị trí trên Google
```

Khi URL/domain được lấy trong quá trình crawl domain khác:

```text
domains.first_source = extracted_from_crawl nếu domain mới
domain_sources.source_type = extracted_from_crawl
domain_sources.source_url_id = URL đang được crawl
domain_sources.discovered_url_id = URL mới được phát hiện nếu có
```

## Rule Engine để extract IOC

Thay vì hard-code toàn bộ logic extract trong backend, hệ thống dùng rule regex lưu trong database.

Flow cụ thể:

1. User tạo rule trên UI.
2. Backend validate regex.
3. Backend lưu rule vào `extraction_rules`.
4. Worker crawl URL.
5. Worker gom dữ liệu đầu vào theo scope: `text`, `html`, `links`, `final_url`, `redirects`.
6. Worker load các rule đang `enabled`.
7. Với mỗi rule, chạy regex trên đúng `input_scope`.
8. Nếu match:
   - Lấy giá trị theo `value_group`.
   - Chạy `exclude_pattern` nếu có.
   - Chuẩn hóa bằng `normalizer`.
   - Lưu IOC vào `iocs`.
   - Lưu nguồn vào `ioc_sources`, kèm `extraction_rule_id`.
9. Nếu IOC type là `url` hoặc `domain`, đưa URL/domain đó quay lại màn review.

### UI tạo rule

Màn hình Rules cần các field:

```text
Rule name
IOC type
Regex pattern
Regex flags
Value group
Input scope
Exclude pattern
Normalizer
Priority
Enabled
Sample text để test
```

Khi user nhập rule, UI nên có nút `Test rule`.

Test rule trả về:

```text
Matched value raw
Matched value normalized
Evidence text
Match count
Error nếu regex sai
Warning nếu regex quá rộng
```

### Validate rule trước khi lưu

Backend không nên lưu regex chưa validate.

Checklist validate:

- Regex compile được.
- `value_group` tồn tại trong pattern.
- Pattern không rỗng.
- Pattern không quá dài, ví dụ tối đa 2.000 ký tự.
- Match timeout, ví dụ 50-200 ms/rule/input.
- Giới hạn số match tối đa trên mỗi rule, ví dụ 500 match/page.
- Không chạy regex trên HTML quá lớn nếu rule chỉ cần `text`.
- Với Node.js nên dùng RE2 để giảm rủi ro ReDoS.
- Với Python nên dùng thư viện `regex` có timeout thay vì `re` thuần nếu cần kiểm soát timeout.

### Rule chính xác hơn regex thô

Để giảm false positive, mỗi rule nên có:

- Boundary rõ ràng: ví dụ `(?<!\d)` và `(?!\d)` cho phone.
- Capture group đúng phần cần lấy.
- `exclude_pattern` để loại placeholder như `example.com`, `test@test.com`.
- `input_scope` hẹp nhất có thể.
- Normalizer phù hợp.
- Evidence text để user kiểm tra lại.

Ví dụ:

```text
Phone rule chỉ nên chạy trên text.
Hash rule có thể chạy trên all.
URL rule nên ưu tiên links/html.
Domain rule nên ưu tiên links/final_url/redirects.
Address rule nên chạy trên text và cần context từ khóa như địa chỉ, address, add, location.
```

### Built-in rules và custom rules

MVP nên có sẵn một bộ rule mặc định:

- Email.
- URL từ text.
- Domain từ text.
- Vietnam phone.
- MD5.
- SHA1.
- SHA256.
- SHA512.
- Address đơn giản.

User có thể:

- Bật/tắt built-in rule.
- Tạo custom rule mới.
- Sửa custom rule.
- Test rule trước khi bật.

Không nên cho user sửa trực tiếp built-in rule. Nếu cần sửa, tạo bản copy thành custom rule.

## Google Dork Template

Google dork không nên hard-code trong backend. User cần cấu hình được trên giao diện giống extraction rule.

### UI tạo dork template

Màn hình Search Dorks cần các field:

```text
Dork name
Template
Enabled
Sample keyword để preview query
Description
```

Template phải có placeholder `{keyword}`.

Ví dụ:

```text
Template: "{keyword}" "contact"
Sample keyword: 88i
Preview query: "88i" "contact"
```

### Validate dork template

Backend cần validate trước khi lưu:

- Template không rỗng.
- Template phải chứa `{keyword}`.
- Template không quá dài, ví dụ tối đa 500 ký tự.
- Sau khi render, query không quá dài, ví dụ tối đa 1.500 ký tự.
- Không cho phép ký tự điều khiển hoặc newline trong template.
- Có preview query để user kiểm tra trước khi chạy.

### Render search query

Pseudo logic:

```python
def render_search_queries(keywords, enabled_dorks):
    for keyword in keywords:
        for dork in enabled_dorks:
            query_text = dork.template.replace("{keyword}", escape_google_query_value(keyword.text))

            search_query_id = db.upsert_search_query(
                keyword_id=keyword.id,
                dork_id=dork.id,
                query_text=query_text,
            )

            db.enqueue_job(
                type="search_query",
                payload={"search_query_id": search_query_id},
            )
```

### API tối thiểu cho Google dork

```text
GET    /api/search-dorks
POST   /api/search-dorks
PUT    /api/search-dorks/:id
POST   /api/search-dorks/:id/enable
POST   /api/search-dorks/:id/disable
POST   /api/search-dorks/preview
DELETE /api/search-dorks/:id
```

Payload preview:

```json
{
  "template": "\"{keyword}\" \"contact\"",
  "sample_keyword": "88i"
}
```

Response preview:

```json
{
  "ok": true,
  "query_text": "\"88i\" \"contact\"",
  "warnings": []
}
```

## Chuẩn hóa tối thiểu

### URL

- Lowercase scheme và host.
- Bỏ fragment `#...`.
- Bỏ port mặc định `:80`, `:443`.
- Bỏ tracking params: `utm_*`, `fbclid`, `gclid`.
- Giữ lại query params khác.

### Domain

- Lowercase.
- Lấy từ hostname của URL.
- Giai đoạn MVP có thể chưa cần tách registered domain bằng Public Suffix List.

### Email

- Lowercase domain.
- Giữ nguyên local-part.

### Phone

- Extract bằng rule regex.
- MVP có thể dùng normalizer `phone_vn`.
- Sau đó nâng cấp sang `libphonenumber`.

### Hash

- MD5: 32 ký tự hex.
- SHA1: 40 ký tự hex.
- SHA256: 64 ký tự hex.
- SHA512: 128 ký tự hex.

### Address

- MVP dùng regex theo keyword context.
- Ví dụ cần có từ khóa gần match: `address`, `địa chỉ`, `dia chi`, `location`, `office`.
- Không nên coi mọi chuỗi dài có số nhà là address nếu thiếu context.
- Lưu raw address và normalized address nếu có normalizer riêng.

## Pseudo code worker

```python
while True:
    job = get_next_pending_job()
    if not job:
        sleep(2)
        continue

    mark_running(job)

    try:
        if job.type == "search_query":
            run_search_query(job.payload["search_query_id"])

        if job.type == "crawl_url":
            run_crawl_url(job.payload["url_id"])

        mark_done(job)

    except Exception as exc:
        mark_failed_or_retry(job, exc)
```

## Pseudo code search

```python
def run_search_query(search_query_id):
    search_query = db.get_search_query(search_query_id)
    page = cloakbrowser.new_page()

    rank = 1
    current_page_no = 1

    while True:
        start = (current_page_no - 1) * 10
        page.goto(
            "https://www.google.com/search?q="
            + encode(search_query.query_text)
            + f"&start={start}&num=10&hl=en"
        )
        results = parse_google_results(page)
        if not results:
            break

        for item in results:
            url_norm = normalize_url(item.url)
            domain = get_domain(url_norm)
            discovered_url_id = db.upsert_url(
                url_raw=item.url,
                url_norm=url_norm,
                domain=domain,
                first_source="google_search",
                review_status="pending_review",
            )
            db.upsert_domain(
                domain=domain,
                first_source="google_search",
                review_status="pending_review",
            )
            db.upsert_url_source(
                url_norm=url_norm,
                source_type="google_search",
                dedupe_key=f"google:{search_query_id}:{current_page_no}:{rank}:{url_norm}",
                keyword_id=search_query.keyword_id,
                search_query_id=search_query_id,
                title=item.title,
                snippet=item.snippet,
                rank=rank,
                page_no=current_page_no,
            )
            db.upsert_domain_source(
                domain=domain,
                source_type="google_search",
                dedupe_key=f"domain:google:{search_query_id}:{current_page_no}:{rank}:{domain}",
                keyword_id=search_query.keyword_id,
                search_query_id=search_query_id,
                discovered_url_id=discovered_url_id,
                rank=rank,
                page_no=current_page_no,
            )
            rank += 1

        current_page_no += 1
```

## Pseudo code crawl

```python
def run_crawl_url(url_id):
    target = db.get_url(url_id)

    if target.review_status != "approved":
        return

    if target.crawl_status == "crawled":
        return

    page = cloakbrowser.new_page()
    response = page.goto(target.url_norm)

    html = page.content()
    text = page.inner_text("body")

    db.mark_crawled(
        url_id=url_id,
        final_url=page.url,
        status_code=response.status,
        html=html,
    )

    extraction_input = {
        "text": text,
        "html": html,
        "links": extract_links_from_dom(page),
        "final_url": page.url,
        "redirects": get_redirect_chain(response),
        "all": html + "\n" + text,
    }

    rules = db.get_enabled_extraction_rules()
    iocs = extract_iocs_by_rules(extraction_input, rules)

    for ioc in iocs:
        ioc_id = db.upsert_ioc(ioc.type, ioc.raw, ioc.norm)
        db.upsert_ioc_source(
            ioc_id=ioc_id,
            source_url_id=url_id,
            extraction_rule_id=ioc.rule_id,
            evidence_text=ioc.evidence,
        )

        if ioc.type == "url":
            domain = get_domain(ioc.norm)
            discovered_url_id = db.upsert_url(
                url_raw=ioc.raw,
                url_norm=ioc.norm,
                domain=domain,
                first_source="extracted_from_crawl",
                review_status="pending_review",
            )
            db.upsert_domain(
                domain=domain,
                first_source="extracted_from_crawl",
                review_status="pending_review",
            )
            db.upsert_url_source(
                url_norm=ioc.norm,
                source_type="extracted_from_crawl",
                dedupe_key=f"crawl:{url_id}:{ioc.norm}",
                source_url_id=url_id,
            )
            db.upsert_domain_source(
                domain=domain,
                source_type="extracted_from_crawl",
                dedupe_key=f"domain:crawl:{url_id}:{domain}",
                source_url_id=url_id,
                discovered_url_id=discovered_url_id,
            )

        if ioc.type == "domain":
            db.upsert_domain(
                domain=ioc.norm,
                first_source="extracted_from_crawl",
                review_status="pending_review",
            )
            db.upsert_domain_source(
                domain=ioc.norm,
                source_type="extracted_from_crawl",
                dedupe_key=f"domain:crawl:{url_id}:{ioc.norm}",
                source_url_id=url_id,
            )
```

## Pseudo code rule extractor

```python
def extract_iocs_by_rules(extraction_input, rules):
    found = []

    for rule in sorted(rules, key=lambda item: item.priority):
        source_text = build_input_by_scope(extraction_input, rule.input_scope)

        matches = safe_regex_finditer(
            pattern=rule.pattern,
            flags=rule.flags,
            text=source_text,
            timeout_ms=100,
            max_matches=500,
        )

        for match in matches:
            raw_value = match.group(rule.value_group)

            if rule.exclude_pattern and regex_search(rule.exclude_pattern, raw_value):
                continue

            norm_value = normalize_by_rule(raw_value, rule.normalizer, rule.ioc_type)

            if not norm_value:
                continue

            evidence = get_context(source_text, match.start(), match.end(), size=80)

            found.append({
                "type": rule.ioc_type,
                "raw": raw_value,
                "norm": norm_value,
                "evidence": evidence,
                "rule_id": rule.id,
            })

    return found
```

## API tối thiểu cho rule

```text
GET    /api/extraction-rules
POST   /api/extraction-rules
PUT    /api/extraction-rules/:id
POST   /api/extraction-rules/:id/enable
POST   /api/extraction-rules/:id/disable
POST   /api/extraction-rules/test
DELETE /api/extraction-rules/:id
```

Payload test rule:

```json
{
  "ioc_type": "phone",
  "pattern": "(?<!\\d)(?:\\+?84|0)(?:3|5|7|8|9)\\d{8}(?!\\d)",
  "flags": "",
  "value_group": 0,
  "input_scope": "text",
  "normalizer": "phone_vn",
  "sample_text": "Liên hệ 0988123456 hoặc support@example.com"
}
```

Response test rule:

```json
{
  "ok": true,
  "matches": [
    {
      "raw": "0988123456",
      "normalized": "+84988123456",
      "evidence": "Liên hệ 0988123456 hoặc support@example.com"
    }
  ],
  "warnings": []
}
```

## UI tối thiểu

Chỉ cần 6 màn hình:

### 1. Keywords

- Textarea nhập keyword.
- Button `Import`.
- Bảng keyword và trạng thái xử lý.
- Chọn danh sách Google dork template sẽ áp dụng.
- Preview số lượng search query sẽ được tạo.

### 2. Search Dorks

- Danh sách Google dork template.
- Tạo/sửa template.
- Bật/tắt template.
- Preview template với sample keyword.
- Ví dụ template: `{keyword}`, `"{keyword}"`, `inurl:{keyword}`, `"{keyword}" "contact"`.

### 3. Review URLs

- Bảng URL.
- Filter theo `pending_review`, `approved`, `rejected`.
- Filter theo domain source: `Google Search`, `From Domain`, `Both`.
- Button `Approve`.
- Button `Reject`.
- Button `Reject domain`.

### 4. Rules

- Danh sách extraction rules.
- Tạo rule regex mới.
- Test rule bằng sample text.
- Bật/tắt rule.
- Filter theo IOC type: phone, email, hash, domain, url, address.

### 5. Crawl Status

- Danh sách URL approved.
- Trạng thái crawl.
- Lỗi nếu có.

### 6. IOC

- Bảng IOC.
- Filter theo type.
- Click một IOC để xem danh sách source URL.
- Hiển thị rule nào đã extract IOC đó.

## Stack dễ triển khai

Phương án đơn giản nhất:

```text
Backend + Worker: Python FastAPI
Browser: CloakBrowser Python API
Database: PostgreSQL
Frontend: React hoặc Next.js
```

Nếu muốn còn đơn giản hơn cho prototype:

```text
Backend + Worker + UI: FastAPI + Jinja/HTMX
Database: SQLite hoặc PostgreSQL
Browser: CloakBrowser Python API
```

Khuyến nghị:

- Prototype dùng SQLite cũng được.
- Bản chạy thật nên dùng PostgreSQL vì cần unique constraint và query tốt hơn.

## Thứ tự làm MVP

1. Tạo database schema.
2. Tạo bảng và màn hình `search_dorks`.
3. Tạo màn hình import keyword và chọn dork template.
4. Render `keyword + dork template` thành `search_queries`.
5. Viết job table và worker loop đơn giản.
6. Viết search bằng CloakBrowser theo `search_query`.
7. Lưu URL/title/snippet/rank/page/query source.
8. Lưu domain và domain source.
9. Tạo màn hình review URL/domain.
10. Tạo bảng `extraction_rules`.
11. Tạo màn hình Rules và chức năng test regex.
12. Viết rule extractor.
13. Viết crawl URL approved.
14. Lưu IOC và IOC sources.
15. Đưa URL/domain mới về review.

## Nâng cấp sau khi MVP chạy ổn

Chỉ thêm các phần này khi thật sự cần:

- Redis Queue thay cho bảng `jobs`.
- S3/MinIO lưu HTML/screenshot thay vì lưu trong DB.
- HAR/network capture.
- Public Suffix List cho domain chính xác hơn.
- `libphonenumber` cho số điện thoại.
- Address parser/geocoder nếu cần chuẩn hóa địa chỉ tốt hơn.
- Search API provider thay cho scrape Google UI.
- Graph visualization.
- Multi-worker và rate limit nâng cao.

## Tiêu chí đạt MVP

- Nhập keyword được.
- Tạo Google dork template được.
- Render keyword + dork thành search query được.
- Search Google theo full query và lưu URL/title được.
- User approve/reject được.
- Crawl URL approved được.
- Tạo và test regex rule trên UI được.
- Backend dùng rule đang enabled để extract IOC.
- Extract được IOC cơ bản: phone, email, hash, domain, URL, address.
- Domain hiển thị được nguồn: từ Google Search hay từ domain/URL khác.
- IOC không bị trùng.
- Xem được một IOC xuất hiện từ những URL nào.
- URL đã crawl không crawl lại.
