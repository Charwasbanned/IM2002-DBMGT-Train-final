# TransitFlow — Database Design Document

**Group Members:**

| Name   | Student ID |
| ------ | ---------- |
| 黃子芹 | 113403048  |
| 蔡芳媛 | 113403045  |
| 陳青嫺 | 113403037  |

---

## Section 1 — Entity-Relationship Diagram

![ERD Diagram](https://i.meee.com.tw/Ni9SjZA.png)

### Entity Overview

<!-- 簡短說明主要 entity 和它們之間的關係，補充圖上看不清楚的地方 -->

| Entity                    | Primary Key              | Description |
| ------------------------- | ------------------------ | ----------- |
| `registered_users`        | `user_id`                | 儲存乘客的基本個人資料（姓名、Email、電話、生日），透過 `is_active` 進行軟刪除以保留稽核記錄 |
| `user_credentials`        | `user_id`                | 儲存使用者的認證資料（argon2id 密碼雜湊、安全問題答案），與 `registered_users` 以 1:1 分開儲存，降低 credential 暴露風險 |
| `metro_stations`          | `station_id`             | 城市捷運網路的站點資料（20 站，涵蓋 M1–M4 四條路線），包含所屬路線及與國鐵換乘站的對應關係 |
| `national_rail_stations`  | `station_id`             | 城際國鐵網路的站點資料（10 站，涵蓋 NR1、NR2 兩條路線），包含與捷運換乘站的對應關係 |
| `metro_schedules`         | `schedule_id`            | 捷運班次時刻表（8 班次，每條路線各 2 方向），包含停靠站順序（`TEXT[]`）、旅行時間（`JSONB`）、票價結構與發車頻率 |
| `national_rail_schedules` | `schedule_id`            | 國鐵班次時刻表（8 班次，含一般與快車服務），包含停靠站順序、旅行時間，以及標準艙與頭等艙的票價結構 |
| `national_rail_seats`     | `(schedule_id, seat_id)` | 國鐵各班次的座位配置，以複合主鍵記錄車廂編號、艙等（standard / first）、排號與欄位，座位屬性只存一份不隨訂票記錄重複 |
| `national_rail_bookings`  | `booking_id`             | 乘客預購的國鐵票券記錄，包含出發/抵達站、行程日期、票種、艙等、指定座位及票價，並以 `status` 追蹤訂票狀態 |
| `metro_travel_history`    | `trip_id`                | 捷運感應進出的乘車紀錄，支援單程票（`single`）與日票（`day_pass`）兩種票種；日票多次搭乘透過 `day_pass_ref` 自我參照串聯 |
| `payments`                | `payment_id`             | 付款交易記錄，以互斥 FK 欄位（`national_rail_booking_id` / `metro_trip_id`）對應國鐵訂票或捷運乘車，保存完整財務稽核記錄 |
| `feedback`                | `feedback_id`            | 乘客搭乘後的評分（1–5）與評論，以互斥 FK 欄位對應國鐵訂票或捷運乘車，以 `ON DELETE RESTRICT` 保留服務品質審核資料 |

---

## Section 2 — Normalisation Justification

### 2.1 Normal Form Design Decisions

**Decision 1：將 `user_credentials` 從 `registered_users` 獨立出來**

在我們的設計中，`registered_users` 只儲存使用者的個人資料（姓名、Email、電話、生日），而驗證相關資料（`password_hash`、`secret_answer_hash`）則獨立存放在 `user_credentials` 表格中，兩張表以 `user_id` 做 1:1 的 Foreign Key 連結。

從 3NF 的角度來看，雖然 `password_hash` 在合併的表格中仍只依賴 `user_id`（candidate key），不存在 transitive dependency，因此技術上不違反 3NF。然而，我們的分離是基於**單一職責原則**的規範化考量：

- `registered_users` 描述「使用者是誰」（identity、profile 資訊）
- `user_credentials` 描述「使用者如何驗證身份」（authentication 資訊）

將這兩類資訊混在同一張表，代表每次查詢使用者 profile（例如顯示姓名、Email）都會不必要地讀取 `password_hash` 欄位，增加了 credential 被意外暴露的風險。分離後可以對兩張表設定不同的存取權限，符合最小權限原則（Principle of Least Privilege）。

---

**Decision 2：將 `national_rail_seats` 獨立成一張表（2NF）**

若座位資料（`coach`、`fare_class`、`seat_row`、`seat_column`）直接嵌入 `national_rail_bookings` 表格，functional dependency 的結構如下：

```
booking_id → user_id, schedule_id, seat_id, travel_date, ...（訂票資訊）
(schedule_id, seat_id) → coach, fare_class, seat_row, seat_column（座位屬性）
```

`seat_row` 和 `seat_column` 只依賴 `(schedule_id, seat_id)` 的組合，而非依賴整個 `booking_id`。這構成了 **Partial Dependency**，違反 **2NF**（Second Normal Form）。

因此我們將座位資料抽取成 `national_rail_seats` 表格，以 `(schedule_id, seat_id)` 作為 composite primary key。這樣：

- `national_rail_bookings` 只儲存與該筆訂票直接相關的資料
- `national_rail_seats` 只儲存與座位本身相關的資料
- 每個 non-key 欄位都完全依賴於整個 primary key → 滿足 **2NF**

同時，這個設計也避免了資料重複：同一個座位若在多筆 booking 中被查詢，`seat_row`、`seat_column` 等資料只存一份，不會因訂票記錄增加而重複儲存。

---

### 2.2 Deliberate De-normalisation Trade-offs

**Decision：`stops_in_order` 使用 `TEXT[]` 陣列而非 Junction Table**

嚴格的 3NF 設計應將站點順序獨立成一張 junction table，例如：

```sql
CREATE TABLE schedule_stops (
    schedule_id VARCHAR(20) REFERENCES metro_schedules(schedule_id),
    stop_order  INTEGER NOT NULL,
    station_id  VARCHAR(10) NOT NULL,
    PRIMARY KEY (schedule_id, stop_order)
);
```

然而我們刻意選擇使用 `TEXT[]` 陣列儲存，理由如下：

1. **靜態資料**：站點順序在 seed 後從不更新，不存在需要對單一 stop 做 INSERT / DELETE 的情境，junction table 的可修改性優勢完全用不到。
2. **讀取效能**：每次查詢 schedule 時都需要站點順序，`TEXT[]` 避免了額外的 JOIN，減少 I/O。在本系統的資料量規模下（16 個 schedules，每個最多 10 站），GIN index 搭配 `ANY()` 運算子的查詢效能與 B-tree JOIN 相當。
3. **查詢模式**：系統的主要查詢是「這班車有沒有停某一站」（containment query），使用 GIN index 可以直接索引整個陣列，達到 O(log n) 查詢。

若將來需要對每一站儲存額外資料（如到站時間、月台編號），才需要改成 junction table。

---

**Decision：`travel_time_from_origin_min` 使用 JSONB 而非獨立表格**

嚴格的 3NF 設計應將旅行時間資料獨立成：

```sql
CREATE TABLE schedule_travel_times (
    schedule_id       VARCHAR(20),
    station_id        VARCHAR(10),
    minutes_from_origin INTEGER NOT NULL,
    PRIMARY KEY (schedule_id, station_id)
);
```

然而我們選擇以 JSONB 格式（`{"MS01": 0, "MS02": 4, "MS03": 9, ...}`）直接儲存在 schedule 表格中，理由如下：

1. **整包讀取**：計算乘客的旅行時間時，永遠需要讀取整個 map，不會只查詢單一站的時間，因此 JSONB 可以一次取得所有資料，不需要多一次 JOIN。
2. **靜態資料**：旅行時間在 seed 後不會更動，JSONB 沒有更新異常（update anomaly）的風險。
3. **複雜度**：獨立一張表會增加 schema 的複雜度，在查詢時需要額外的 JOIN 和 GROUP BY，對現有的查詢模式沒有帶來實質好處。

---

### 2.3 Password Hashing

**Algorithm：argon2id**

本系統的密碼（`password_hash`）與安全問題答案（`secret_answer_hash`）均使用 **argon2id** 演算法進行雜湊儲存，實作參數為 `time_cost=2`、`memory_cost=65536`（64 MB）、`parallelism=2`。

---

**WHY argon2id but not MD5 or SHA-1？**

MD5 和 SHA-1 是**通用雜湊演算法**，設計目標是速度快。在現代 GPU 上，MD5 每秒可以計算超過 **100 億次**雜湊，這讓暴力破解（brute force）和字典攻擊（dictionary attack）變得非常容易——攻擊者只需對每個候選密碼計算 hash 並比對即可。

argon2id 的核心差異在於它是**密碼專用的慢速雜湊演算法（Password Hashing Function）**，刻意設計成計算代價高昂：

- **Time Cost**：控制 hash 的迭代次數，每次計算需要更多 CPU 時間
- **Memory Cost**：強迫計算過程佔用大量 RAM（本系統設定 64 MB），讓 GPU 平行運算的優勢大幅降低，因為 GPU 顯存通常不夠同時跑大量高記憶體需求的 hash
- **Memory-Hard**：即使使用專用 ASIC 硬體加速，Memory Cost 的限制依然有效

因此，同樣的計算資源下，攻擊者能測試的密碼候選數量只有 MD5/SHA-1 的幾千分之一，暴力破解成本大幅提高。

---

**Salt 如何防止 Rainbow Table 攻擊？**

Rainbow Table 是預先計算好的「密碼 → hash」對應表，攻擊者取得資料庫後可以直接查表找到原始密碼，而不需要重新計算。

argon2-cffi 在每次呼叫 `ph.hash()` 時，會**自動為每個使用者產生一個隨機 salt**，並將 salt 值嵌入最終輸出的 hash 字串中（格式：`$argon2id$v=19$m=65536,...$<salt>$<hash>`）。salt 不需要保密，它的作用是讓每個人的 hash 都不同：

- 即使 User A 和 User B 設定了完全相同的密碼，因為 salt 不同，儲存的 `password_hash` 也會完全不同
- 攻擊者無法事先建立通用的 Rainbow Table，因為每個 hash 都對應一個不同的 salt，等於需要為每個使用者分別建表
- 這徹底使預先計算的查表攻擊失效

---

## Section 3 — Graph Database Design Rationale

### 3.1 What is Stored as Nodes, Relationships, and Properties

**Nodes（節點）**

| Node Label            | Stored Properties                                                                                                        | 設計理由                                                                                                                                                                                                                                        |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MetroStation`        | `station_id`, `name`, `lines`, `is_interchange_metro`, `interchange_metro_lines`, `is_interchange_national_rail`         | 站點是路網中的離散實體（discrete entity），代表乘客可以上下車的地點。每個站點有固定的屬性（名稱、所在路線），天然對應圖的節點概念。                                                                                                             |
| `NationalRailStation` | `station_id`, `name`, `lines`, `is_interchange_national_rail`, `interchange_national_rail_lines`, `is_interchange_metro` | 與 metro 站分開成兩個 label，而非用單一 `Station` 加 `network_type` 屬性，原因是：Cypher 可以用 label 做高效的 node scan（`MATCH (s:MetroStation)`），不需要每次過濾 `network_type`；兩種站點有不同的 interchange 屬性結構，分開 label 更清晰。 |

我們選擇用兩個不同的 node label 而非一個 `Station` 加 `network_type` 屬性的設計，是因為 Neo4j 使用 label 建立內部索引。分開的 label 讓路線查詢可以限定在 metro 或 rail 網路內，避免每次都要掃描全部節點再過濾類型。

---

**Relationships（關係）**

| Relationship Type | 方向                                           | Properties                                        | 設計理由                                                                                                                                                                                                                                            |
| ----------------- | ---------------------------------------------- | ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `METRO_LINK`      | `MetroStation` → `MetroStation`                | `line`（路線名稱）, `travel_time_min`（行駛時間） | 兩個相鄰站之間的直達連線。每個 `adjacent_stations` 條目對應一條有向邊；由於資料是對稱的（A→B 和 B→A 都存在），路網可以雙向走訪。`travel_time_min` 作為 Dijkstra 的 cost 權重。                                                                      |
| `RAIL_LINK`       | `NationalRailStation` → `NationalRailStation`  | `line`, `travel_time_min`                         | 與 `METRO_LINK` 相同設計原則，應用於國鐵網路。用不同的 relationship type 讓 Cypher 可以精確指定只走 metro 或只走 rail，不會誤跨網路。                                                                                                               |
| `INTERCHANGE_TO`  | `MetroStation` ↔ `NationalRailStation`（雙向） | 無 properties                                     | 跨網路換乘點，連線的存在本身就是事實（edge existence as fact）。換乘時間（5 分鐘）不存在 edge 上，而是在 APOC Dijkstra 呼叫時透過 `defaultCost=5` 傳入：`apoc.algo.dijkstra(a, b, 'METRO_LINK\|RAIL_LINK\|INTERCHANGE_TO', 'travel_time_min', 5)`。 |

`INTERCHANGE_TO` 故意不存 properties ：若將換乘時間存在 edge 上，日後若不同換乘站的等待時間不同，需要分別更新；用 `defaultCost` 統一管理，修改只需一個地方。

---

**Properties（屬性）**

Properties 存在 node 或 relationship 上，補充描述實體或連線的靜態資訊：

- **node properties**：`station_id`（唯一識別）、`name`（顯示名稱）、`lines`（所屬路線陣列）用於路徑結果的人類可讀輸出。`is_interchange_*` 屬性用於快速判斷節點是否為換乘站，不需要額外的 edge 查詢。
- **relationship properties**：`travel_time_min` 是路徑最佳化的核心權重，`line` 用於在結果中告知乘客每一段應搭哪條線。

---

### 3.2 Why Graph Database is Better than Relational for Routing

**最短路徑查詢（Shortest Path）**

在 Neo4j 中，最短路徑查詢只需一行 APOC 呼叫：

```cypher
CALL apoc.algo.dijkstra(start, end, 'METRO_LINK', 'travel_time_min')
YIELD path, weight
```

APOC Dijkstra 在內部使用 priority queue（min-heap），時間複雜度為 **O((V + E) log V)**，其中 V 是節點數、E 是邊數。圖的adjacency 已經原生表達在 relationships 中，不需要任何 JOIN。

若要在 PostgreSQL 中實作同等的加權最短路徑，需要：

```sql
WITH RECURSIVE path_search AS (
    SELECT origin_id AS station_id, 0 AS total_time, ARRAY[origin_id] AS visited
    UNION ALL
    SELECT adj.station_id,
           ps.total_time + adj.travel_time_min,
           ps.visited || adj.station_id
    FROM path_search ps
    JOIN station_adjacency adj ON adj.from_station = ps.station_id
    WHERE NOT adj.station_id = ANY(ps.visited)   -- 防止 cycle
),
ranked AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY total_time) AS rn
    FROM path_search WHERE station_id = destination_id
)
SELECT * FROM ranked WHERE rn = 1;
```

這個 recursive CTE 每次遞迴都需要與 adjacency 表格做一次 JOIN，並手動維護 `visited` 陣列來防止 cycle，不僅實作複雜，在深度路徑下效能也顯著下降（沒有 priority queue，無法做早停）。

---

**延誤擴散查詢（Delay Ripple）**

在 Neo4j 中，找出延誤站 N 跳範圍內的所有站點，用 variable-length traversal 直接表達：

```cypher
MATCH (disrupted {station_id: $station_id})
MATCH path = (disrupted)-[:METRO_LINK|RAIL_LINK|INTERCHANGE_TO*1..3]-(affected)
WHERE affected.station_id <> $station_id
WITH affected, min(length(path)) AS hops_away
RETURN affected.station_id, affected.name, hops_away
```

`*1..3` 這個語法直接對應「最多 N 跳」的概念，Neo4j 會自動追蹤已走過的節點防止無限循環。

若在 SQL 中實作同樣的 N-hop 查詢，需要 N 層 self-JOIN 或者 recursive CTE 加上深度計數，而且每增加一跳就要修改查詢結構，不具有彈性。

---

### 3.3 Two Query Types Explained

**Query 1：最短路徑 `query_shortest_route`**

這個 query 找兩站之間旅行時間最短的路徑。

圖的結構如何支援這個查詢：

- 每條 `METRO_LINK` / `RAIL_LINK` 上的 `travel_time_min` 就是邊的權重
- APOC Dijkstra 沿 relationship 逐步展開，累加 `travel_time_min`，優先探索目前累計時間最小的路徑
- 因為路網是有向圖（每段連線都有明確方向），Dijkstra 可以高效地找到全域最短時間路徑

結果回傳的 `path`（每個節點的 station_id 和 name）和 `legs`（每一段的 from/to/line/time）直接從 relationship 的 properties 中取出，不需要額外 JOIN 其他表格。

---

**Query 2：延誤擴散分析 `query_delay_ripple`**

這個 query 找出在延誤站 N 跳範圍內所有可能受影響的站點，並回報每個站距離延誤站幾跳。

圖的結構如何支援這個查詢：

- Variable-length pattern `*1..{hops}` 同時走訪 `METRO_LINK`、`RAIL_LINK`、`INTERCHANGE_TO` 三種 relationship，因此可以跨越 metro 和 national rail 網路的邊界，正確模擬跨網路的延誤擴散
- `min(length(path))` 計算到每個受影響站的最短跳數，代表最直接的影響路徑
- 當 `hops=0` 時（只回傳延誤站本身），因為 `*1..0` 在 Cypher 中是無效語法，實作上用獨立的 `MATCH (s {station_id: $station_id})` 處理此邊界情況

---

### 3.4 Node Identity

每個節點以 `station_id` 屬性作為唯一識別，並在資料庫層建立 uniqueness constraint：

```cypher
CREATE CONSTRAINT metro_station_unique IF NOT EXISTS
FOR (s:MetroStation) REQUIRE s.station_id IS UNIQUE

CREATE CONSTRAINT rail_station_unique IF NOT EXISTS
FOR (s:NationalRailStation) REQUIRE s.station_id IS UNIQUE
```

選擇 `station_id`（而非 `name`）的原因：

- 站點名稱可能有拼字變體或縮寫（例如 "Central Sq" vs "Central Square"），而 `station_id` 是格式固定的業務代碼（`MS01`–`MS20`、`NR01`–`NR10`），不存在歧義
- `station_id` 與 PostgreSQL relational schema 中的 primary key 完全一致（`metro_stations.station_id`、`national_rail_stations.station_id`），讓跨資料庫查詢（例如 `query_cheapest_route` 在 Neo4j 找到路徑後去 PostgreSQL 查票價）可以直接用同一個 ID 對應，不需要額外的 mapping
- uniqueness constraint 讓 `MERGE` 操作成為 idempotent，seeder 重複執行不會產生重複節點

---

## Section 4 — Vector / RAG Design

### 4.1 What is Embedded and Why Cosine Similarity

**嵌入的資料：Policy Documents**

系統將四類政策文件嵌入（embed）後儲存於 PostgreSQL 的 `policy_documents` 表格中，供使用者查詢退票規則、票種說明、訂票規範、乘車規定等問題：

| 來源檔案               | Category  | 說明                                         |
| ---------------------- | --------- | -------------------------------------------- |
| `refund_policy.json`   | `refund`  | 各情境的退票政策（例如延誤退款、取消退款）   |
| `ticket_types.json`    | `booking` | 各票種的詳細說明（single、return、day pass） |
| `booking_rules.json`   | `booking` | metro 和 national rail 的訂票規則            |
| `travel_policies.json` | `conduct` | 乘車行為規範（行李、寵物、設備使用）         |

每份文件以 JSON 文字格式（`json.dumps`）作為嵌入輸入，確保結構化欄位（如退款條件、適用期限）都被模型捕捉到。

---

**為什麼使用 Cosine Similarity**

Cosine similarity 測量的是兩個向量在嵌入空間中的**方向相似度**，而非距離大小（magnitude）。計算方式為：

```

cosine_similarity(A, B) = (A · B) / (||A|| × ||B||)
```

這個特性使其「magnitude-independent（不受向量長度影響）」：一份三段話的退票政策和一份十段話的退票政策，即使向量長度（norm）不同，只要語意方向相近，cosine similarity 仍然高。相比之下，若用歐氏距離（Euclidean distance）則會因為長文件的向量 norm 更大而產生偏差。

語意搜尋的本質是問「這個問題的語意方向和哪份文件最相近？」，而非「哪份文件的向量值最接近？」，因此 cosine similarity 是更合適的相似度度量。

在實作中，pgvector 的 `<=>` 運算子計算的是 **cosine distance**（= 1 − cosine similarity），`ORDER BY embedding <=> query_vector` 等同於從最相似到最不相似排列：

```sql
SELECT title, category, content,
       1 - (embedding <=> %s::vector) AS similarity
FROM policy_documents
WHERE 1 - (embedding <=> %s::vector) > 0.3
ORDER BY embedding <=> %s::vector
LIMIT 3
```

---

### 4.2 Full RAG Pipeline

RAG（Retrieval-Augmented Generation）的核心概念是：讓 LLM 只根據**從資料庫中實際取出的文件**來回答問題，而不是依靠 LLM 訓練時記住的知識（可能過時或不準確）。TransitFlow 的 RAG pipeline 分為四個階段：

---

**Stage 1 — Query Embedding（查詢向量化）**

使用者的自然語言問題（例如「可以退票嗎？」）先送入 embedding model 轉換成一個浮點數向量：

```python
# agent.py — search_policy tool handler
embedding = llm.embed(params["query"])
```

`llm.embed()` 根據啟動時設定的 `LLM_PROVIDER` 呼叫對應的模型：

- Ollama：POST 至 `http://localhost:11434/api/embeddings`，使用 `nomic-embed-text` 模型，回傳 768 維向量
- Gemini：呼叫 `gemini-embedding-001`，回傳 3072 維向量

---

**Stage 2 — Similarity Search（相似度搜尋）**

將查詢向量送進 PostgreSQL，用 `<=>` cosine distance 運算子與所有已儲存的 policy document embedding 比較，取出相似度最高的前 K 份文件：

```python
docs = query_policy_vector_search(embedding)
```

底層 SQL 使用 HNSW 索引（`CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)`）進行 Approximate Nearest Neighbor（ANN）搜尋，在精度與速度之間取得平衡，並設有相似度門檻（`> 0.3`）過濾不相關結果。

---

**Stage 3 — Context Injection（上下文注入）**

取出的文件（最多 3 份，每份截取前 800 字元）連同 `title`、`category`、`similarity score` 被包裝成 tool result 回傳給 agent：

```python
result = [
    {
        "title":      d["title"],
        "category":   d["category"],
        "content":    d["content"][:800],
        "similarity": round(d["similarity"], 3),
    }
    for d in docs
]
```

這些文件成為 LLM 在下一階段生成答案時的唯一事實依據（ground truth），而非讓 LLM 憑記憶回答。

---

**Stage 4 — LLM Answer Generation（答案生成）**

Agent 將原始使用者問題和所有 tool results（包含政策文件內容）一起傳給 LLM（Gemini 或 Ollama），由 LLM 根據這些具體文件內容組織成自然語言回答。System prompt 明確指示：「When DATA FROM TRANSITFLOW DATABASE is provided, use it as the only source of truth.」確保 LLM 不會輸出資料庫以外的資訊。

---

### 4.3 Embedding Dimension and Provider Switching

**本實作使用的 embedding 維度**

我們的預設實作使用 **768 維** embedding（Ollama `nomic-embed-text` 模型）。`policy_documents` 表格的 `embedding` 欄位宣告為 `vector(768)`（定義於 schema.sql），HNSW 索引也以 768 維建立。

若切換至 Gemini `gemini-embedding-001`，輸出維度為 **3072 維**（由 `GEMINI_EMBED_DIM` 設定）。

---

**切換 Provider 後會發生什麼事**

`embed_provider`（決定用哪個模型做 embedding）在程式啟動時固定，**不隨 chat provider 的切換而改變**：

```python
# llm_provider.py
self._embed_provider = LLM_PROVIDER  # 啟動時設定，之後不變
self.embed_dim = OLLAMA_EMBED_DIM if self._embed_provider == "ollama" else GEMINI_EMBED_DIM
```

如果在用 Ollama（768 維）seed 完資料後，將 `.env` 的 `LLM_PROVIDER` 改為 `gemini` 並重新啟動，再執行 `search_policy`：

1. 使用者問題會被 Gemini 嵌入成 **3072 維**向量
2. 但 `policy_documents.embedding` 欄位是 `vector(768)`
3. PostgreSQL 的 `<=>` 運算子要求兩個向量維度相同，會拋出 `ERROR: different vector dimensions`，整個搜尋失敗

**解決方式**：切換 provider 後必須執行 `docker-compose down -v && docker-compose up -d` 清空資料庫，然後修改 schema.sql 的 `vector(768)` 為 `vector(3072)`，再重新 `seed_vectors.py`，才能讓所有 embedding 使用一致的維度。`seed_vectors.py` 內建了維度驗證，如果 embedding 輸出和 `llm.embed_dim` 不符會立即中止。

---

## Section 5 — AI Tool Usage Evidence


### Example 1 — Designing Mutually Exclusive Foreign Keys for the Payments Table

**Context:** 我們需要一個 payments 表，能且只能在每筆付款紀錄連到 national_rail_bookings 或 metro_travel_history 其中一方，以避免重複表並保留單一付款紀錄。

**Prompt:** 
「在資料庫中實作「互斥外鍵」模式：在 payments 表保留兩個可為 null 的 FK 欄位（national_rail_booking_id, metro_trip_id），並加上 CHECK 條件確保恰好一個非 null；另外在欄位上加註解說明互斥行為。」

**Outcome:** 我會在 schema 中加入 CHECK ((national_rail_booking_id IS NULL) != (metro_trip_id IS NULL))，並在 payments 表欄位上加上註解；用 seed 資料測試後，該約束能正確阻止同時指向兩者或兩者皆為空的情形。

---

### Example 2 —  Implementing RF001/RF002 Refund Window Logic in execute_cancellation

**Context:** 老師提供的 execute_cancellation() docstring 明確要求根據 service type 計算退款金額，並列出兩套退款時間窗口政策：RF001和 RF002。最初的實作只是將 booking status 改為 'cancelled'、payment status 改為 'refunded'，沒有依任何政策計算退款金額，也沒有回傳規格要求的 refund_amount_usd 和 policy_note。

**Prompt:** 
1. 「現在的 execute_cancellation 實作和規定要求的相比，具體缺少了哪些部分」
2. 「@train-mock-data/refund_policy.json 根據政策定義，幫我實作退款金額計算邏輯 先取得 booking 的 service_type 和 departure_time，計算距出發的小時數後套用對應的退款窗口，最後在回傳中加入 refund_amount_usd 和 policy_note」

**Outcome:** AI 在 train-mock-data/refund_policy.json 找到 RF001 和 RF002 的完整定義（各窗口的時間條件、退款百分比、手續費）。對照後確認組員的實作完全缺少退款計算邏輯。最終實作以 JOIN national_rail_schedules 取得 service_type，用 datetime.combine 計算 hours_until，正確套用兩套政策並回傳 refund_amount_usd 和 policy_note。

---

### Example 3 — Identifying a Silent Bug in the operates_on Day-of-Week Filter

**Context:** query_national_rail_availability() 接受 travel_date 參數時，會將日期轉換成星期幾後去比對 operates_on 陣列，篩選當天有運行的班次。這段邏輯看起來沒有問題，但我們在對照 schema.sql 和 agent.py 做全面審查時，請 AI 一併確認 seed 資料的實際格式是否一致。

**Prompt:** 
1. 「請對照 schema.sql、agent.py、AI_SESSION_CONTEXT.md 全面審查目前的 queries.py，找出還有沒有潛在的問題。」
2. 「query_national_rail_availability() 使用 strftime("%A") 取得星期名稱，請查看 train-mock-data/national_rail_schedules.json 確認 seed 資料的 operates_on 欄位實際存的是什麼格式。」

**Outcome:** AI 最初在全面審查時評估 operates_on 的 filter 邏輯為正確，沒有立即發現問題。然而在被追問確認 seed 資料格式後，AI 解析 JSON 檔案，發現 seed 資料存的是小寫縮寫 ['mon', 'tue', 'wed', ...]，而 strftime("%A") 輸出的是完整英文名稱如 "Monday"。這個不一致導致傳入 travel_date 時篩選無法命中，會靜默回傳空陣列卻不拋出任何錯誤。AI 確認後，將其修正為 strftime("%a").lower()，使輸出與 seed 資料格式一致。

---

### Example 4 — Choosing argon2id Parameters and Verifying Case-Insensitive Secret Answer

**Context:** 實作 `register_user()` 和 `verify_secret_answer()` 時，我們使用 argon2-cffi 函式庫進行密碼雜湊，但不確定 `time_cost`、`memory_cost`、`parallelism` 三個參數應該設多少才合理，也不確定安全問題答案是否需要做大小寫正規化。

**Prompt:**
「argon2id 的 time_cost=2、memory_cost=65536、parallelism=2 這些參數合理嗎？memory_cost 的單位是什麼？還有如果安全問題答案需要case insensitive，要在 hash 之前還是 hash 之後處理？」

**Outcome:** AI 說明 `memory_cost` 的單位是 KiB（所以 65536 = 64 MB），並確認上述參數是 OWASP 建議的最低合規值，在一般伺服器硬體上每次 hash 約需 50–100ms，足以抵抗暴力破解且不影響使用者體驗。對於大小寫不敏感的問題，AI 建議在 hash 之前先呼叫 `.lower()` 正規化，因為 argon2id 是確定性函式——相同輸入永遠產生可驗證的輸出——因此 `register_user` 存入時和 `verify_secret_answer` 驗證時都必須套用同樣的正規化，確保「Paris」和「paris」會被視為相同答案。我們依此將 `secret_answer.lower()` 加入兩個函式，並用測試確認大小寫不同的輸入都能通過驗證。

---

## Section 6 — Reflection & Trade-offs

### 6.1 Design Decisions

**Decision 1：對交通基礎設施表格使用 Natural Key（VARCHAR）而非 SERIAL**

`metro_stations`、`national_rail_stations`、`metro_schedules`、`national_rail_schedules` 等表格全部使用業務代碼（`MS01`–`MS20`、`NR_SCH01` 等）作為 primary key，而非讓 PostgreSQL 自動產生的 SERIAL 整數。

具體理由：

- **與 source data 一致**：mock data JSON 已使用這套 ID 格式，seed 程式可直接插入原始資料，不需要維護「JSON ID → DB SERIAL」的映射表
- **跨資料庫一致性**：Neo4j 的 `station_id` property 與 PostgreSQL 的 `station_id` PK 使用相同 ID，`query_cheapest_route` 在 Neo4j 找到路徑後可直接用同一個 ID 去 PostgreSQL 查票價，不需要額外 JOIN
- **可讀性**：客服人員或開發者看到 `schedule_id = 'NR_SCH03'` 就能直接理解；若用 SERIAL，`schedule_id = 7` 需要額外查表才有意義

代價是 VARCHAR JOIN 比 INTEGER JOIN 稍慢，但在本系統規模（20 + 10 個站點）下，差異可忽略不計。

---

**Decision 2：使用 Soft Delete（`is_active = FALSE`）而非物理刪除使用者**

`registered_users` 表格設有 `is_active BOOLEAN DEFAULT TRUE`，帳號停用時只將此欄位設為 `FALSE`，不執行 `DELETE`。

具體理由：

- **保留稽核記錄**：`national_rail_bookings` 和 `metro_travel_history` 都以 `user_id` 作為 FK 指向 `registered_users`，設定為 `ON DELETE RESTRICT`。若物理刪除使用者，這些訂票與乘車記錄的 FK 會因 RESTRICT 被拒絕刪除，或若用 CASCADE 則會連同財務記錄一起刪除，兩者都不可接受
- **支援帳號恢復**：軟刪除的帳號可以直接把 `is_active` 改回 `TRUE` 即可恢復，不需要重新輸入資料
- **財務記錄完整性**：付款記錄（`payments`）和使用者回饋（`feedback`）應永久保存供日後查核，即使使用者已停用帳號

---

### 6.2 What Would Be Different in a Production System

**Schema Migration 工具（取代直接重建資料庫）**

目前的開發流程是：每次修改 `schema.sql` 就執行 `docker-compose down -v && docker-compose up -d` 清空整個資料庫再重建。這在開發環境可行，但在 production 完全不可接受：`-v` 旗標會刪除所有 Docker volume，包含真實用戶的訂票記錄、付款歷史、帳號資料，一旦執行就是不可逆的資料遺失。

Production 環境應改用 **Alembic**（Python schema migration 工具）管理 schema 演進。每次結構變更產生一個 migration 檔案，記錄 `upgrade` 和 `downgrade` 操作：

```python
# migrations/versions/0003_add_booking_index.py
def upgrade():
    op.create_index('idx_bookings_user_travel_date',
                    'national_rail_bookings', ['user_id', 'travel_date'])

def downgrade():
    op.drop_index('idx_bookings_user_travel_date')
```

Alembic 追蹤資料庫目前處於哪個 migration version，只執行尚未套用的變更，不需要清空資料。這樣 schema 可以在 production 做 rolling update，保留所有現有資料的同時完成結構調整。
