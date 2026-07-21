# BYOK — 用你自己的帳號，五分鐘拿到完整設定

> **台灣人的 AI token 自由**：帳號你自己去申請，任何人五分鐘都拿得到。
> 真正拿不到的是**設定**——那組不會把 prompt cache 弄壞的 model alias、
> session affinity、以及讓客戶端真的吃到 1M 上下文的開關。
> 這個資料夾就是把那組設定交到你手上，一行指令。

```bash
python byok/setup.py
```

---

## 先講清楚：yangble5 不是模型

* yangble5 **不是**一個模型，**不是**台灣訓練的 LLM，**不是**微調，**不是**免費額度的來源。
* yangble5 是一組**設定 + 量測工具 + 相容層**，架在第三方開源 Go 專案
  [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)（MIT，**不是我們寫的**）上面，
  讓 Claude Code / Codex 這類 coding agent 可以走同一個端點接到 Gemini / Grok / GPT。
* 每一個 token 都記在**你自己設定的那個上游帳號**上。這裡沒有人幫你出錢。

自由指的是**接取的自由**，不是「免費」。

---

## 共用池 vs BYOK：誠實比較

專案的共用池是營運者自掏腰包撐的，容量是真的小（目前只有**一個**健康的上游帳號）。
它存在的意義是「零設定、馬上試試看」，不是「拿來當主力」。

| | 共用池 | **BYOK（這個資料夾）** |
|---|---|---|
| 設定成本 | 零。拿到邀請碼就能用 | 約五分鐘：申請帳號 + 跑一次 setup |
| 容量 | **很小，先到先得**。人多就排隊或被限流 | 你自己帳號的完整額度，**不會被別人用完** |
| 額度是誰的 | 營運者的 | 你的 |
| 池空了會怎樣 | 降級或擋掉。不會有「無限」這種事 | 不受影響 |
| 出問題找誰 | 要等營運者 | 你自己就能重跑、重測、改設定 |
| 資料經過誰的機器 | 營運者的伺服器 | **只有你自己的機器**（`127.0.0.1`） |
| 拿得到快取設定嗎 | 拿得到（在服務端） | 拿得到，**而且是你可以讀、可以改的檔案** |
| 適合誰 | 想先看看值不值得的人 | **任何真的要拿來寫程式的人** |

**如果你打算認真用，BYOK 是比較好的選擇，而且沒有很難。**
共用池請當成試用，不要當成基礎設施——它撐不住，這點我們不騙你。

---

## 五分鐘流程

### 步驟 1：拿到你自己的上游帳號

`setup.py` 支援三種：

| 種類 | `--provider` | 你要準備什麼 |
|---|---|---|
| Google Gemini（API key） | `gemini-api-key` | 到 Google AI Studio 登入、建一把 API key |
| 任何 OpenAI 相容端點 | `openai-compat` | API key + base URL（通常結尾是 `/v1`）+ **精確的**上游模型名稱 |
| OAuth 通道（antigravity / gemini-cli / codex / claude / kimi / xai） | `oauth` | 跑 CLIProxyAPI 自己的登入流程 |

> **OAuth 這條路，腳本不會幫你自動點瀏覽器。**
> 自動化別人的登入頁既脆弱、也不是一個安裝腳本該做的事。
> 腳本會把步驟印出來，你自己完成登入，它**盯著 auth 目錄**，一偵測到 token 檔就繼續。

免費額度、模型可用性、限流規則都是上游說了算，隨時會變，而且我們跟這些供應商沒有任何關係。
**所以這份文件不會出現任何「一天幾塊錢額度」之類的數字。**

### 步驟 2：跑 setup

互動模式（推薦第一次用）：

```bash
python byok/setup.py
```

腳本模式（CI、重灌、或你已經知道要什麼）：

```bash
export YANGBLE5_UPSTREAM_KEY='你的上游 key'      # 只從環境變數讀
python byok/setup.py \
  --provider gemini-api-key \
  --model gemini-2.5-pro \
  --non-interactive
```

> **key 永遠不會出現在命令列參數。**
> argv 在多數系統上其他行程讀得到，而且會被寫進 shell history。
> 所以 `setup.py` 只從環境變數或隱藏輸入拿 key——`--api-key` 這個 flag 根本不存在，
> 這件事有測試在守（`test_the_parser_has_no_flag_that_accepts_a_credential`）。

先看它會做什麼、但什麼都不寫：

```bash
python byok/setup.py --dry-run
```

### 步驟 3：驗證

setup 的最後一步會跑 `tools/cache_bench.py`，把**實際量到的**快取命中率印出來。

* 命中率達標 → 它會說 PASS，**並且提醒你這只是單機單次的暖輪數字**。
* 命中率不達標 → 它**不會**說成功，會直接印診斷清單（見下方「疑難排解」）。

引擎還沒起來的話，它會等你（預設最多 90 秒），等不到就老實說「沒東西可以量」，
然後把該跑的指令印給你。**設定寫成功 ≠ 設定有效**，這條線我們不模糊。

### 步驟 4：開始用

```bash
# 1) 起引擎（CLIProxyAPI 執行檔要你自己準備）
cli-proxy-api --config ~/.yangble5/byok/config.yaml

# 2) 載入環境變數
. ~/.yangble5/byok/env.sh          # PowerShell 用 env.ps1

# 3) 如果引擎版本 < 7.2.93，Claude Code 還需要 shim
python tools/claude_shim.py --listen-host 127.0.0.1 --listen-port 8320 \
       --upstream http://127.0.0.1:8318

# 4) 開工
claude
codex
```

---

## setup 到底寫了哪些檔案

預設全部寫在 `~/.yangble5/byok/`，**完全不碰這個 repo，也不碰你原本的 Claude Code / Codex 設定**。

| 檔案 | 內容 | 有沒有機密 |
|---|---|---|
| `config.yaml` | 引擎設定（快取設定已經調好） | **有**（API key 模式下的上游 key），模式 0600 |
| `env.sh` / `env.ps1` | 環境變數 | **有**（本機 proxy key），模式 0600 |
| `claude/settings.json` | 獨立的 Claude Code 設定目錄 | 沒有 |
| `codex/config.toml` | 獨立的 `CODEX_HOME` | 沒有（key 走 `env_key`） |
| `auth/` | OAuth token 檔（引擎自己寫） | **有**，目錄 0700 |

規則：

* **任何既有檔案都會先備份再改。** 備份檔名帶時間戳，同一秒內跑兩次也不會互相覆蓋
  （會變成 `-1`、`-2`）。內容完全一樣的話則整個跳過，不動 mtime。
* 讀不懂的 `settings.json`（壞掉的 JSON）**不會**被靜靜覆蓋——腳本會停下來叫你自己處理。
* 已經存在的 `settings.json` 只會被合併我們自己那幾個 `env` 鍵；你的 permissions、hooks、
  statusLine 一個都不會掉。
* 機密只寫進「本來就是拿來裝機密」的那兩個檔案。`settings.json` 和 `config.toml` 裡沒有 key。

> Windows 上 `chmod` 沒辦法表達「只有擁有者能讀」。腳本仍然會呼叫它（對的平台上有效），
> 但在 Windows 上**檔案權限不是保證**。這點我們寧可講清楚，也不要假裝有。

---

## 為什麼「設定」才是重點

### 三個保住快取的設定

上游的 prompt cache 是按**憑證**分開的，而且只要 prompt 前綴變了就是 miss。
所以真正決定成本的不是提示詞技巧，是這三件事：

1. **1:1 的 model alias（最重要）**
   在 CLIProxyAPI 7.1.23，同一個 alias 如果對到**兩個**上游模型名稱，那不是設定錯誤，
   而是一個「內部 model pool」功能：每次請求會用一個**全域計數器**
   （`conductor.go` 的 `nextModelPoolOffset`）輪流換上游。
   那個計數器**同時無視 `routing.strategy` 和 `session-affinity`**。
   結果就是同一場對話的連續幾輪打到不同上游、不同快取，命中率被鎖死在大約 1/N。

   `setup.py` **拒絕**產生這種設定，這是 `AliasPoolError`，有測試守著。

2. **`routing.strategy: fill-first`**
   預設的 round-robin 會把一場對話拆到不同憑證上，而快取是跟著憑證走的。

3. **`routing.session-affinity: true` + 長 TTL（預設 12h）**
   把一場 session 釘在同一個憑證上。TTL 太短的話，你去吃個午餐回來就是一次冷輪。

三個都寫在 `byok/config.template.yaml` 裡，每一行都有註解說明**為什麼**。

### 客戶端的 1M 解鎖

你的 alias 依定義就是一個**沒有任何客戶端聽過**的模型名稱。
客戶端遇到不認識的名字會猜一個保守的視窗，然後照著猜的動作：

* **Claude Code** 假設 200K，於是很早就開始 auto-compact——而每一次 compact
  都是一次會把快取打掉的 prompt 重寫。
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`（官方環境變數，v2.1.193+）把那條線移開。
* **Codex** 對應的是 `config.toml` 裡的 `model_context_window = 1000000`。

> **把數字設大不會憑空變出上下文。** 它只改變客戶端「什麼時候決定壓縮」。
> 如果上游吃不下你接著送過去的東西，你只是把「太早壓縮」換成「截斷或報錯」。
> 所以這一條跟驗證是綁在一起的：用 `cache_bench.py` 確認你宣稱的大小真的沒被截。

---

## 實測數字，以及它們不代表什麼

以下是這個專案唯一有實測的量化數字（2026-07-21，**一台 Windows 11、每組設定只跑一次**）：

| 量測 | 數值 |
|---|---|
| 暖輪（第 2–4 輪）token 加權快取命中率 | **99.53%** |
| 冷輪（每個 session 的第一次請求） | **0%** |
| 單一 prompt 處理且未截斷 | **748,918 tokens** |
| 該 prompt 大小下的延遲，冷 → 暖 | **21.4s → 10.8s** |
| Claude Code 端到端成功 | **3/3** |

**請一起讀這些但書，不要單獨引用上面的數字：**

1. **99.53% 是暖輪數字。** 每一個 session 的第一次請求都是 0% 的冷寫入，這是結構性的，
   沒有任何設定能避免。
2. **命中率跟前綴大小有關。** 未命中的尾巴大致是固定的，所以前綴越大比例越好看：
   同一套堆疊在 749K 前綴量到 99.53%，在 91K 量到 94.00%。
   `setup.py` 預設用 60K 驗證（不想讓你第一次就付一輪 750K 的錢），
   所以**你看到的數字大概率會低於 99.53%，那是正常的**。
3. **一台機器、一次跑。** 沒有重複測量、沒有信賴區間、沒有跨供應商比較。
   上游隨時會改快取行為。這就是為什麼我們是把量測工具給你，而不是叫你相信我們。
4. **沒有即時網路搜尋。** 實測：透過這條路問「今年是哪一年」，Gemini 回 2024、Grok 回 2025。
   需要即時資訊的工作，這條路不適合。
5. **shim 是暫時的。** `tools/claude_shim.py` 只是把上游 7.2.93 的修正（把對話中途的
   `role: "system"` 訊息改成 `user`）往回移植到 7.1.23。引擎升到 7.2.93 以上就
   `--no-shim`，把這一跳拿掉。
6. **CLIProxyAPI 不是我們的作品。** 引擎是別人的優秀開源專案，我們貢獻的是設定、
   量測、相容層和這個安裝流程。

---

## 疑難排解：命中率很低怎麼辦

`setup.py` 量到低命中率時會直接印這份清單，這裡是同一份：

1. **alias 在設定裡是不是只出現一次？** 出現兩次就變成 pool，per-request 輪換上游，
   `routing.strategy` 和 `session-affinity` 都管不到它。這是最常見的原因。
2. **`routing.strategy` 是不是 `fill-first`？**
3. **`session-affinity` 有沒有開？TTL 有沒有比你離開座位的時間長？**
4. **引擎中間是不是重啟過？** affinity 表在記憶體裡，重啟就清空。
5. **前綴是不是太小？** 先用 `--bench-prefix-tokens 200000` 再測一次，再下結論。
6. **上游到底有沒有回報任何 cached token？** 如果每一輪都是 0，那是這個上游在這條路徑上
   不提供快取計費資訊、或根本不快取。**那是上游的性質，不是設定 bug**，
   應該照實說「這裡做不到」，而不是繼續調。
7. **客戶端是不是在壓縮？** 沒設 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 的話，Claude Code
   會很早開始 compact，而 compact 就是 prompt 重寫，重寫就是 miss。

手動重測：

```bash
. ~/.yangble5/byok/env.sh
python tools/cache_bench.py --model yangble5 --rounds 4 --prefix-tokens 200000
```

---

## 安全

* 引擎預設只綁 `127.0.0.1`。它**沒有**任何 per-user 計費或限流，
  所以任何連得到那個 port 的東西都能把你的額度花光。
  想開給別人用的話，不要改那一行——把 `gateway/` 擺在前面，並且先讀 `deploy/`。
* 管理 API（`/v0/management/*`）預設關閉（`secret-key: ""`）。它能列出、發放憑證並改寫設定。
* `debug: false`。debug log 會把完整請求內容寫進檔案——也就是你的原始碼。
* 產生出來的 `config.yaml` 和 `env.*` 帶機密。它們預設就在 repo 外面，
  而且 repo 的 `.gitignore` 也擋 `config.yaml`、`.env`、`auth/`。**不要貼到 issue 裡。**

---

## FAQ

**Q：我一定要有 CLIProxyAPI 執行檔嗎？**
要。`setup.py` 不會幫你下載或安裝第三方執行檔——要不要把某個 binary 放上你的機器，
那是你的決定，不是安裝腳本的。到
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 取得並自己驗 checksum。

**Q：我原本的 Claude Code 設定會被蓋掉嗎？**
不會。setup 寫的是**獨立的** `CLAUDE_CONFIG_DIR` 和 `CODEX_HOME`，
而且任何既有檔案都會先備份。不載入 `env.sh` 的時候，你的原本設定完全沒變。

**Q：可以同時設好幾個上游嗎？**
可以，但請**一個 alias 對一個模型**。想用兩個模型就給兩個 alias
（例如 `yangble5` 和 `yangble5-flash`）。把兩個模型塞進同一個 alias 正是本文一直在講的那個坑。

**Q：這樣就有 1M 上下文了嗎？**
我們實測到 **748,918 tokens 沒有被截斷**。1,000,000 **沒有量過**，所以我們不會這樣宣稱。
而且「吃得下」不等於「記得住」——我們沒有做 needle-in-a-haystack 的召回測試。

**Q：能連網搜尋嗎？**
不能。見上面第 4 點。

---

## In English

`byok/setup.py` configures a **local** CLIProxyAPI instance against **your own** upstream
account, with the cache-preserving settings already correct: a strict 1:1 model alias (never a
same-alias multi-model pool — that shape makes the engine rotate upstreams per request via a
global counter that ignores both `routing.strategy` and session affinity), `fill-first` routing,
session affinity with a 12h TTL, loopback-only bind, plus the client-side context unlock
(`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`, `model_context_window = 1000000`) in an isolated
Claude Code config dir and `CODEX_HOME`.

It never accepts a credential on argv, never writes outside `~/.yangble5/byok` by default, backs
up every file it touches, and finishes by running `tools/cache_bench.py` and printing the real
measured hit rate — or a diagnostic checklist if that number is low.

```bash
export YANGBLE5_UPSTREAM_KEY='...'
python byok/setup.py --provider gemini-api-key --model gemini-2.5-pro --non-interactive
```

Read the caveats above before quoting any number: 99.53% is a **warm-round** figure from a
**single run on one machine**, every session's first request is a 0% cold write, the rate is
prefix-size dependent, there is no live web search through this path, and the engine itself
([CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), MIT) is **somebody else's work**.

---

## 致謝

引擎是 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)（router-for-me，MIT）。
`byok/` 裡的東西只是設定、驗證和安裝流程。沒有那個引擎，這裡什麼都不成立。
