# StudyMate AI Pro — Cập nhật (Giao diện kiểu ChatGPT/Claude + Tài khoản)

## Những gì vừa thêm so với bản trước

### 1. Hệ thống tài khoản (đăng ký / đăng nhập / đăng xuất)
- Trang **`/register`** và **`/login`** riêng, giao diện đồng bộ với thương hiệu StudyMate.
- Mật khẩu được băm bằng `werkzeug.security` (PBKDF2), **không lưu plaintext**.
- Đăng nhập dùng session cookie (HttpOnly, SameSite=Lax) — Flask ký cookie bằng `SECRET_KEY`.
- Toàn bộ trang chat (`/`) và các API (`/api/chat`, `/api/upload`, `/api/conversations*`) đều yêu cầu đăng nhập; gọi API mà chưa đăng nhập sẽ nhận `401` (frontend tự chuyển hướng về `/login`).
- Dữ liệu tài khoản lưu trong **SQLite** (`studymate.db`, tự tạo cạnh `app.py`, không cần cài thêm gì).

⚠️ Lưu ý quan trọng: thêm `SECRET_KEY=<chuỗi_ngẫu_nhiên_dài>` vào file `.env`. Nếu không đặt, mỗi lần restart server người dùng sẽ bị đăng xuất hết (vì khóa ký session bị sinh lại ngẫu nhiên) — server sẽ tự in ra một gợi ý khi khởi động.

### 2. Lịch sử trò chuyện theo tài khoản (giống ChatGPT/Claude)
- Mỗi tin nhắn được lưu vào bảng `conversations` / `messages` trong SQLite, gắn với `user_id`.
- Sidebar bên trái liệt kê các đoạn chat cũ, bấm vào để mở lại toàn bộ hội thoại.
- Nút **"Đoạn chat mới"** để bắt đầu hội thoại mới; icon thùng rác để xoá đoạn chat (có xác nhận trước khi xoá).
- Đoạn chat mới được tạo tự động ngay khi gửi tin nhắn đầu tiên (tiêu đề lấy từ nội dung câu hỏi đầu tiên).

### 3. Giao diện được thiết kế lại theo phong cách các AI phổ biến (ChatGPT/Claude)
- Bố cục 2 cột: **sidebar** (lịch sử chat + tài khoản) và **khung chat chính**, thay cho layout 4 cột kiểu dashboard cũ.
- Tin nhắn của học sinh hiển thị dạng bong bóng bên phải; câu trả lời AI hiển thị dạng văn bản trơn kèm avatar robot bên trái (không còn bong bóng gradient nặng nề) — giống cách ChatGPT/Claude trình bày.
- Môn học và Chế độ học tập chuyển thành 2 ô chọn dạng "pill" gọn gàng ở thanh trên cùng, thay vì chiếm hẳn một cột lớn.
- Ô nhập câu hỏi dạng thanh bo tròn nổi ở dưới cùng (giống thanh composer của ChatGPT/Claude), có nút đính kèm, nút micro và nút gửi hình mũi tên.
- Sidebar tự thu gọn trên di động (menu hamburger), có overlay khi mở.
- Khu vực tài khoản ở cuối sidebar: avatar chữ cái đầu + tên đăng nhập + menu "Đăng xuất".
- Vẫn giữ nguyên toàn bộ tính năng cũ: streaming trả lời theo thời gian thực, đọc PDF/Word/txt/csv, đọc ảnh, kéo-thả file, dark/light mode, trợ lý giọng nói.

### 4. Tài khoản Developer + Trang thống kê sử dụng (mới)
- Bảng `users` có thêm cột **`role`** (`user` mặc định, hoặc `developer`).
- Khi khởi động lần đầu, server **tự tạo 1 tài khoản developer**:
  - Tên đăng nhập mặc định: `developer` (đổi bằng biến `DEVELOPER_USERNAME` trong `.env`).
  - Nếu chưa đặt `DEVELOPER_PASSWORD` trong `.env`, server tự sinh mật khẩu ngẫu nhiên và **in ra console đúng 1 lần** khi khởi động — hãy đăng nhập và đổi mật khẩu ngay (đổi bằng cách xoá dòng tương ứng trong bảng `users` rồi tạo lại, hoặc tự thêm chức năng đổi mật khẩu sau).
  - Nếu database cũ đã có sẵn tài khoản tên `developer`, server sẽ **tự nâng quyền** tài khoản đó thành `developer` (không tạo trùng).
- Đăng nhập bằng tài khoản này sẽ thấy mục **"Thống kê (Developer)"** trong menu tài khoản ở sidebar (👤 → góc dưới sidebar), dẫn tới trang **`/developer`**.
- Trang `/developer` hiển thị:
  - Tổng số tài khoản, tổng lượt hỏi AI, lượt hỏi hôm nay / 7 ngày qua, tỉ lệ lỗi.
  - Biểu đồ cột số lượt sử dụng theo từng ngày (14 ngày gần nhất).
  - Phân bổ lượt hỏi theo **môn học** và theo **chế độ học tập**.
  - Danh sách người dùng hoạt động nhiều nhất + toàn bộ danh sách tài khoản.
  - **Không lưu/hiển thị nội dung câu hỏi hay câu trả lời** — chỉ số liệu tổng hợp (độ dài, môn học, chế độ, trạng thái thành công/lỗi), lưu trong bảng mới `usage_logs`.
- Route `/developer` được bảo vệ bởi decorator `developer_required`: người dùng thường (`role = user`) sẽ bị chuyển hướng về trang chat; chưa đăng nhập sẽ bị chuyển tới `/login`.

⚠️ Lưu ý: đây là cơ chế "role" đơn giản (một cột trong SQLite), phù hợp cho dự án cá nhân/lớp học. Nếu deploy công khai, nên bổ sung: giới hạn số lần thử đăng nhập (rate limit), trang đổi mật khẩu, và log audit khi có người truy cập `/developer`.

## Cài đặt & chạy

```bash
pip install -r requirements.txt
```

Bản này thêm thư viện `Authlib` (cho đăng nhập Google) và `gunicorn` (để chạy production) vào `requirements.txt`.

Tạo file `.env` cùng thư mục:

```
XAI_API_KEY=xai-xxxxxxxxxxxxxxxx
CONSOLEX_API_BASE=https://api.x.ai/v1
CONSOLEX_MODEL=grok-4.5
SECRET_KEY=mot-chuoi-ngau-nhien-that-dai-va-kho-doan
```

Muốn bật thanh toán thật cho Premium/Max, xem thêm biến `.env` cho VNPAY/VietQR ở mục 14.

Chạy:

```bash
python app.py
```

Truy cập `http://localhost:5000` → sẽ tự chuyển tới `http://localhost:5000/login` nếu chưa đăng nhập. Bấm "Đăng ký ngay" để tạo tài khoản đầu tiên.

## 5. Đăng nhập bằng Google (mới)

- Trang `/login` và `/register` giờ có giao diện mới (nền gradient, glassmorphism) và **tự động hiện nút "Đăng nhập với Google"** nếu bạn đã cấu hình Client ID/Secret trong `.env`. Chưa cấu hình thì nút tự ẩn, app vẫn chạy bình thường với tên đăng nhập/mật khẩu như cũ.
- Tài khoản tạo qua Google được lưu trong cùng bảng `users`, cột `password_hash` để trống — **nghĩa là server không bao giờ nắm giữ, xem, hay lưu mật khẩu Google thật của người dùng**; toàn bộ việc xác thực diễn ra ở phía Google, app chỉ nhận lại `id`, `email`, `tên hiển thị` sau khi người dùng đồng ý.
- Nếu người dùng đăng nhập bằng cùng email đã có tài khoản mật khẩu trước đó, tài khoản đó sẽ được **liên kết thêm** OAuth thay vì tạo trùng.

### Lấy Google Client ID / Secret
1. Vào [Google Cloud Console](https://console.cloud.google.com/) → tạo project mới (hoặc chọn project có sẵn).
2. Vào **APIs & Services → OAuth consent screen** → chọn "External" → điền tên app, email → lưu.
3. Vào **APIs & Services → Credentials → Create Credentials → OAuth client ID** → chọn "Web application".
4. Ở mục **Authorized redirect URIs**, thêm:
   - `http://localhost:5000/auth/google/callback` (để test local)
   - `https://tenmiencuaban.com/auth/google/callback` (khi deploy thật — thay bằng domain thật của bạn)
5. Copy `Client ID` và `Client secret`, dán vào `.env`:
   ```
   GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=xxxxxxxx
   ```

(Muốn thêm Apple Sign In sau này: quy trình tương tự nhưng cần tài khoản Apple Developer trả phí 99 USD/năm và cấu hình phức tạp hơn — cho mình biết nếu bạn muốn làm tiếp phần này.)

## Lưu ý bảo mật (đã cân nhắc nhưng không phóng đại)
- Mật khẩu băm bằng PBKDF2 (`werkzeug.security`), không hard-code, không lưu plaintext.
- Session cookie có `HttpOnly` + `SameSite=Lax`.
- Input validation cơ bản cho form đăng ký (độ dài tên đăng nhập, độ dài mật khẩu tối thiểu, kiểm tra khớp mật khẩu nhập lại).
- API key vẫn chỉ đọc từ biến môi trường.
- Giới hạn upload: `MAX_CONTENT_LENGTH = 15MB` tổng, `6MB` riêng cho ảnh.
- **Chưa có**: rate limiting cho đăng nhập/đăng ký (nên thêm `flask-limiter` nếu deploy public để chống brute-force), CSRF token riêng cho form (hiện dựa vào `SameSite=Lax` + same-origin), xác thực email, quên mật khẩu. Đây là những phần nên bổ sung trước khi đưa lên môi trường thật với nhiều người dùng.

## 6. Public app lên thành website chính thức (Deploy)

App hiện chạy bằng `app.run(...)` — chỉ phù hợp để **test trên máy cá nhân**, không nên dùng khi có người dùng thật. Dưới đây là 2 cách phổ biến để đưa app lên mạng.

### Cách A — Nhanh nhất: Render.com (miễn phí, khuyên dùng cho dự án cá nhân/lớp học)

1. Đưa toàn bộ code (`app.py`, `requirements.txt`, ...) lên một repo GitHub.
2. Vào [render.com](https://render.com) → đăng nhập bằng GitHub → **New → Web Service** → chọn repo vừa tạo.
3. Cấu hình:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT`
4. Vào tab **Environment**, thêm toàn bộ biến trong `.env` của bạn (XAI_API_KEY, SECRET_KEY, GOOGLE_CLIENT_ID, ...) — **không commit file `.env` lên GitHub**.
5. Bấm **Deploy**. Render sẽ cấp cho bạn 1 domain dạng `https://ten-app.onrender.com` kèm HTTPS miễn phí sẵn.
6. Quay lại Google Cloud Console, thêm `https://ten-app.onrender.com/auth/google/callback` vào danh sách Redirect URI (bước ở mục 5 phía trên).
7. (Tuỳ chọn) Gắn domain riêng của bạn trong tab **Settings → Custom Domain** của Render.

⚠️ Lưu ý: gói miễn phí của Render dùng ổ đĩa tạm — file `studymate.db` (SQLite) **có thể bị mất khi server khởi động lại**. Với dự án thật có nhiều người dùng, nên: (a) nâng cấp gói có "Persistent Disk", hoặc (b) chuyển sang PostgreSQL (Render có sẵn dịch vụ Postgres miễn phí, cần sửa lại phần kết nối DB trong `app.py`).

### Cách B — Tự chủ hơn: VPS riêng (DigitalOcean, Vultr, AWS Lightsail...)

1. Thuê 1 VPS Ubuntu (rẻ nhất khoảng 4-6 USD/tháng), trỏ domain của bạn về IP của VPS (bản ghi A).
2. SSH vào VPS, cài Python, clone code, tạo virtualenv, `pip install -r requirements.txt`, tạo file `.env`.
3. Chạy app bằng gunicorn làm service nền (systemd), ví dụ file `/etc/systemd/system/studymate.service`:
   ```
   [Unit]
   Description=StudyMate AI Pro
   After=network.target

   [Service]
   WorkingDirectory=/duong/dan/toi/studymate
   ExecStart=/duong/dan/toi/studymate/venv/bin/gunicorn app:app --bind 127.0.0.1:8000 --workers 3
   Restart=always
   EnvironmentFile=/duong/dan/toi/studymate/.env

   [Install]
   WantedBy=multi-user.target
   ```
   Sau đó: `sudo systemctl enable --now studymate`.
4. Cài Nginx làm reverse proxy (chuyển tiếp từ cổng 80/443 vào 127.0.0.1:8000), rồi cài `certbot` để lấy chứng chỉ HTTPS miễn phí (Let's Encrypt): `sudo certbot --nginx -d tenmiencuaban.com`.
5. Cập nhật Redirect URI ở Google thành `https://tenmiencuaban.com/auth/google/callback` như ở Cách A bước 6.

Cách B tốn công cấu hình hơn nhưng bạn toàn quyền kiểm soát dữ liệu (file SQLite không bị mất khi restart) và không giới hạn tài nguyên như gói miễn phí.

## 7. Sửa lỗi hiển thị (mới)
- **Chữ tiếng Việt bị vỡ ký tự khi AI trả lời**: nguyên nhân do thư viện `requests` tự đoán sai encoding của response streaming từ xAI (mặc định ISO-8859-1 thay vì UTF-8). Đã ép `resp.encoding = 'utf-8'` trong `stream_consolex_ai()` — lỗi này đã hết.
- **Công thức toán hiện ra dạng chữ thô** (`$$...$$`, `\begin{cases}`...): app trước đó chưa có bộ render LaTeX. Đã thêm **KaTeX** (qua CDN) để tự động render công thức đẹp, và dặn AI trong system prompt luôn dùng đúng cú pháp `$$...$$` / `\(...\)`.
- **Chữ tràn ra ngoài khung chat**: đã thêm `overflow-wrap: anywhere` cho phần nội dung AI trả lời để tự xuống dòng với chuỗi dài không có khoảng trắng.
- **Đã bỏ đăng nhập Facebook** khỏi toàn bộ app (route, nút bấm, biến môi trường) — chỉ còn đăng nhập bằng Google + tên đăng nhập/mật khẩu.

## 8. Dự án, Ghim, Tìm kiếm, Cài đặt cá nhân (mới)

Sidebar được nâng cấp thêm cấu trúc kiểu ChatGPT/Claude:

- **Ô tìm kiếm** phía trên sidebar: lọc đoạn chat theo tiêu đề ngay khi gõ (client-side, không cần reload).
- **Dự án**: bấm dấu "+" cạnh mục "Dự án" để tạo một dự án mới (vd: "Ôn thi HK1"), sau đó dùng menu "⋯" trên từng đoạn chat để **chuyển đoạn chat vào dự án**. Bấm vào tên dự án trong sidebar để lọc riêng các đoạn chat thuộc dự án đó.
- **Ghim đoạn chat**: menu "⋯" trên từng đoạn chat có mục Ghim/Bỏ ghim — các đoạn đã ghim nổi lên mục "Đã ghim" riêng, luôn ở trên cùng.
- **Đổi tên đoạn chat**: cũng nằm trong menu "⋯".
- **Menu tài khoản** (góc dưới sidebar) có thêm 3 mục mới:
  - **Cài đặt**: chọn giao diện Sáng/Tối/Theo hệ thống, ngôn ngữ (Tiếng Việt/English — dịch nhẹ phần khung giao diện, không dịch nội dung AI trả lời), môn học & chế độ mặc định khi mở đoạn chat mới, và nút **xoá toàn bộ lịch sử** (có xác nhận trước khi xoá).
  - **Trợ giúp & phím tắt**: liệt kê các phím tắt — `Ctrl/Cmd+K` đoạn chat mới, `Ctrl/Cmd+/` mở trợ giúp, `Esc` đóng hộp thoại.
  - **Nâng cấp gói**: chỉ là bản xem trước giao diện, **chưa có chức năng thanh toán thật** — ghi rõ trong hộp thoại để tránh gây hiểu lầm.
- Tất cả tuỳ chọn ở trên được lưu theo tài khoản (cột `preferences` mới trong bảng `users`, dạng JSON) — đăng nhập ở máy khác vẫn giữ nguyên cài đặt.
- **Banner thông báo hệ thống**: nếu developer bật banner (xem mục 9), mọi người dùng đã đăng nhập sẽ thấy dòng thông báo trong sidebar, có thể bấm "x" để tạm ẩn (ẩn theo phiên trình duyệt, hiện lại nếu mở tab mới hoặc banner được đổi nội dung).

## 9. Công cụ quản lý mới cho tài khoản Developer

Trang `/developer` có thêm mục **"Quản lý hệ thống"**:

- **Banner thông báo**: ô nhập nội dung (tối đa 300 ký tự) + công tắc bật/tắt, bấm "Lưu banner" để áp dụng ngay cho toàn bộ người dùng.
- **Công tắc đăng nhập Google (runtime)**: chỉ hiện nếu `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` đã có trong `.env`. Cho phép Bật/Tắt/Về mặc định nút "Đăng nhập với Google" **ngay lập tức, không cần sửa `.env` hay khởi động lại server** — hữu ích khi cần tạm khoá đăng nhập Google (vd: đang debug OAuth) mà không ảnh hưởng đăng nhập bằng mật khẩu.
- **Xuất CSV**: tải toàn bộ `usage_logs` (không có nội dung câu hỏi/trả lời, chỉ số liệu) ra file `usage_logs.csv` để phân tích thêm bằng Excel/Google Sheets.
- **Tìm kiếm tài khoản**: ô tìm theo tên đăng nhập phía trên bảng "Toàn bộ tài khoản".
- **Thăng/hạ quyền developer trực tiếp từ bảng tài khoản**: nút "Nâng lên developer" / "Hạ xuống user" trên từng dòng. Có 2 lớp bảo vệ:
  - Không thể tự hạ quyền chính tài khoản đang đăng nhập.
  - Không thể hạ quyền nếu đó là **developer cuối cùng** của hệ thống — luôn phải còn ít nhất 1 tài khoản developer.

⚠️ Không cần cài thêm thư viện nào cho các tính năng ở mục 8–9 — toàn bộ dùng lại `flask`, `sqlite3`, `csv` (thư viện chuẩn của Python), không cập nhật `requirements.txt`.

## 10. Gói sử dụng: Free / Premium / Max (mới)

> ⚠️ Lưu ý: bản `app.py` bạn gửi lần này là một nhánh phát triển khác với các bản trước (đã có sẵn hệ thống vai trò `user → developer → admin → super_admin`, AI Tutor tuỳ chỉnh, nhật ký audit, v.v. — xem mục 13). Mục 10–11 dưới đây là phần mình vừa hoàn thiện theo đúng yêu cầu mới nhất của bạn (mục 10 nay đã cập nhật để khớp với cổng thanh toán thật ở mục 14). "Báo lỗi câu trả lời", "Bộ nhớ AI" và điểm thưởng/chuỗi ngày học (gamification) **đã có sẵn** trong nhánh này — xem mục 16.

Đã bỏ hẳn chữ **"Pro"** khỏi mọi nơi hiển thị cho tài khoản thường — tên app giờ đổi động theo gói:

| Gói | Ai có | Giới hạn đọc file/ảnh mỗi 24h | Dung lượng tối đa/file | Tên app hiển thị |
|---|---|---|---|---|
| 🆓 **Free** | Mặc định mọi tài khoản mới | 20 lượt | 20MB | `StudyMate AI` |
| 💎 **Premium** | Admin gán tay từ `/developer` | 50 lượt | 500MB | `StudyMate AI Premium` |
| 🚀 **Max** | Admin gán tay, **hoặc tự động nếu vai trò ≥ Developer** | Không giới hạn | 1GB | `StudyMate AI Max` |

- Cột `plan` mới trong bảng `users` (mặc định `'free'`) lưu gói do Admin gán thủ công — **nhưng** tài khoản có vai trò `developer`/`admin`/`super_admin` luôn được tính là **Max vô điều kiện** ngay cả khi cột `plan` vẫn ghi `'free'` (hàm `effective_plan()` ưu tiên vai trò trước, không cần Admin phải gán tay cho từng dev). Vì vậy trang `/developer` **không cho đổi gói** với các tài khoản từ Developer trở lên (nút bị ẩn, kèm chú thích lý do).
- Giới hạn *số lượt* đọc file/ảnh tính theo **cửa sổ trượt 24 giờ** (không phải theo lịch nửa đêm reset) — bảng mới `file_uploads` ghi lại mỗi lượt tải lên, cứ quá 24h thì lượt đó "hết hạn" và tự nhường chỗ cho lượt mới. Giới hạn dung lượng còn siết luôn cả bước trích chữ từ PDF/Word: Free cắt ở ~12.000 ký tự, Premium ~48.000 ký tự, Max không cắt.
- **Đổi gói (Admin trở lên)**: bảng "Toàn bộ tài khoản" ở `/developer` có thêm cột **Gói** + dropdown đổi gói tại chỗ (`POST /developer/users/<id>/plan`), có ghi audit log. Đổi sang Free thì xoá hạn dùng ngay; đổi sang Premium/Max thì **chỉ tặng đúng 1 tháng miễn phí** (dùng chung hàm `grant_plan_upgrade()` với cổng thanh toán thật ở mục 14, không phải gán vĩnh viễn) — hết hạn tự rơi về Free như một lượt nâng cấp bình thường.
- **Xem gói + hạn mức của chính mình**: `GET /api/plan` trả về gói hiện tại, hạn dùng còn lại (nếu là gói trả phí), đã dùng bao nhiêu/bao nhiêu lượt hôm nay, có phải "miễn phí theo vai trò" không, và có đang được hưởng ưu đãi lần đầu không. Hiển thị ngay trong **Cài đặt** (thanh tiến trình nhỏ, tự làm mới sau mỗi lần tải file).
- **Hộp thoại "Nâng cấp gói"**: hiện đúng 3 cột Free/Premium/Max với số liệu thật, tự khoanh viền cột gói hiện tại của người xem, liệt kê các Chế độ suy nghĩ mở khoá ở từng gói (xem mục 11), và **giờ có cổng thanh toán thật** — xem chi tiết ở mục 14.

## 11. Chế độ suy nghĩ của AI: Trợ Lý / Học Giả / Giáo Sư / Thiên Tài (mới)

Một dropdown mới ngay cạnh 2 ô "Môn học" / "Chế độ" ở thanh trên cùng, cho học sinh chọn AI nên "đầu tư" bao nhiêu công sức suy luận cho câu trả lời:

| Chế độ | Icon | Gói tối thiểu | Ngân sách token | Ý tưởng |
|---|---|---|---|---|
| Trợ Lý | 💬 | Free | 800 | Mặc định, nhanh, cân bằng |
| Học Giả | 📖 | Premium | 1.400 | Suy luận từng bước kỹ hơn trước khi chốt đáp án |
| Giáo Sư | 🎓 | Premium | 1.600 | Giải thích mở rộng — nhiều ví dụ, liên hệ thực tế |
| Thiên Tài | 🌟 | **Max** (độc quyền) | 2.200 | Kết hợp cả suy luận sâu lẫn giải thích mở rộng — mạnh nhất |

- Học Giả + Giáo Sư tương ứng đúng 2 chế độ "deepthinking"/"extra" bạn yêu cầu (mở khoá từ Premium); Thiên Tài là chế độ **độc quyền Max** duy nhất, kết hợp cả hai — bạn có thể đổi tên hiển thị bất cứ lúc nào ở dict `THINKING_MODES` trong `app.py`, không cần sửa logic.
- Trong dropdown, chế độ chưa mở khoá vẫn hiện đầy đủ (kèm mô tả) nhưng có khoá 🔒 + nhãn gói cần có — bấm vào sẽ mở thẳng hộp thoại "Nâng cấp gói" thay vì chọn được.
- **Chặn ở cả server, không chỉ ẩn ở giao diện**: kể cả khi ai đó tự gọi thẳng `POST /api/chat` với `thinkingMode: "genius"` mà tài khoản đang là Free, server vẫn tự động hạ về `"standard"` (hàm `resolve_thinking_mode()`) — không tin tưởng dữ liệu phía client gửi lên.
- Chế độ đang chọn được gắn thêm 1 đoạn hướng dẫn (`prompt_hint`) vào system prompt và đổi luôn `max_tokens` gửi cho model — không tốn thêm lượt gọi API nào ngoài 1 lượt chat bình thường.

⚠️ Chưa lưu chế độ suy nghĩ đang chọn vào tuỳ chọn cá nhân (`preferences`) — mỗi lần tải lại trang sẽ về lại "Trợ Lý" mặc định. Muốn nhớ lựa chọn qua các lần đăng nhập thì cần thêm 1 field vào `DEFAULT_PREFERENCES`/`get_preferences()`/`set_preferences()` — có thể làm tiếp nếu bạn cần.

## 12. Sửa 1 lỗi ẩn khi kết hợp streaming + SQLite (mới, quan trọng)

Trong lúc kiểm thử tính năng ở mục 10–11, phát hiện một lỗi có sẵn từ trước (không liên quan tới gói/chế độ suy nghĩ, nhưng ảnh hưởng tới **mọi** câu trả lời AI): các lượt chat thật ra bị lỗi ngầm **"Cannot operate on a closed database"** ngay khi AI bắt đầu trả lời.

**Nguyên nhân:** `/api/chat` dùng `stream_with_context()` để giữ `request`/`session`/`g` sống trong lúc trả lời dạng streaming (SSE) — nhưng cơ chế này **không** ngăn được `teardown_appcontext` (hàm đóng kết nối SQLite `g._database`) chạy sớm hơn generator thật sự bắt đầu. Kết quả: bất kỳ chỗ nào trong generator (hoặc trong các hàm nó gọi tới, kể cả gián tiếp — ví dụ `stream_consolex_ai()` đọc cấu hình model/temperature qua `get_setting()`) mà dùng lại `get_db()`/`g` đều đụng phải kết nối SQLite **đã bị đóng từ trước**.

**Đã sửa:** thêm hàm `open_write_db()` — mở 1 kết nối SQLite **độc lập, không qua `g`**, tự đóng ngay sau khi dùng xong. Áp dụng cho mọi thao tác ghi DB xảy ra **bên trong** generator streaming: lưu câu trả lời của AI vào lịch sử chat, ghi `usage_logs` (`log_usage()`), và đọc cấu hình runtime (`get_setting()`/`set_setting()`, vì `stream_consolex_ai()` gọi tới 2 hàm này để lấy model/temperature ghi đè). Đã kiểm thử lại toàn bộ luồng chat (thành công lẫn báo lỗi) — không còn gặp lỗi này nữa.

## 13. Vai trò & công cụ quản trị đã có sẵn trong nhánh này (ghi chú lại, không phải mình làm)

Để tránh trùng lặp tài liệu, đây là danh sách nhanh những gì `app.py` bạn gửi **đã có sẵn** trước khi mình động vào (mình chỉ dùng/nối thêm vào, không viết lại): hệ thống vai trò 4 cấp `user → developer → admin → super_admin` (`ROLE_ORDER`/`role_rank()`), khoá/mở khoá tài khoản, reset session, xoá tài khoản, AI Tutor tuỳ chỉnh (`/api/tutors`), API key cá nhân (`/api/keys`, `/api/v1/ping`), Playground thử prompt cho Developer trở lên, ghi đè model/temperature/system-prompt chung không cần restart, chế độ bảo trì, và nhật ký audit (`/developer/audit`, chỉ Super Admin xem được). Nếu cần tài liệu chi tiết cho từng phần này, cho mình biết để viết bổ sung.

## 14. Nâng cấp gói: thanh toán thật theo tháng + ưu đãi lần đầu (mới)

Gói Premium/Max giờ là **thuê bao theo THÁNG** (không còn "gán vĩnh viễn"), có 2 cách thanh toán:

| Gói | Giá gốc | Ưu đãi lần đầu |
|---|---|---|
| 💎 Premium | 30.000đ/tháng | **50%** cho 3 tháng đầu → 15.000đ/tháng |
| 🚀 Max | 50.000đ/tháng | **50%** cho 3 tháng đầu → 25.000đ/tháng |

- **Ưu đãi lần đầu**: đếm theo tổng số đơn **đã thanh toán thành công** trong lịch sử tài khoản (`payment_orders.status = 'paid'`), không phân biệt Premium hay Max — đủ 3 đơn thì từ đơn thứ 4 trở đi tính giá bình thường, không cần làm gì thêm. Chỉnh mức % hoặc số tháng ưu đãi ở 2 hằng số `FIRST_TIME_DISCOUNT_PCT` / `FIRST_TIME_DISCOUNT_MONTHS` đầu file `app.py`.
- **Hết hạn tự rơi về Free**: mỗi lượt thanh toán chỉ cấp đúng 1 tháng (cột `plan_expires_at` mới trong bảng `users`), tính lại từ **thời điểm thanh toán** (không cộng dồn nếu gia hạn sớm). Hết hạn mà chưa thanh toán tiếp thì `effective_plan()` tự trả về Free ngay lần tải trang kế tiếp — không cần cron job/background task nào.
- **2 phương thức thanh toán**, cấu hình qua `.env` (thiếu biến nào thì phương thức đó tự ẩn khỏi giao diện, không lỗi):
  ```
  # VNPAY (thẻ ATM nội địa/Visa/Mastercard/JCB) — đăng ký merchant tại https://vnpay.vn
  VNPAY_TMN_CODE=xxxxxxxx
  VNPAY_HASH_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  VNPAY_PAYMENT_URL=https://sandbox.vnpayment.vn/paymentv2/vpcpay.html   # đổi sang URL production khi go-live

  # Chuyển khoản VietQR (quét mã QR bằng app ngân hàng/MoMo/ZaloPay) — không cần đăng ký merchant
  VIETQR_BANK_ID=mbbank          # tên ngân hàng hoặc mã BIN, xem danh sách tại vietqr.io
  VIETQR_ACCOUNT_NO=xxxxxxxxxxxx
  VIETQR_ACCOUNT_NAME=NGUYEN VAN A
  ```
  - VNPAY chốt đơn **tự động** qua IPN (`vnpay_ipn()`), có xác thực chữ ký HMAC-SHA512 — không tin bất kỳ tham số nào từ trình duyệt gửi lên.
  - Chuyển khoản VietQR chốt đơn **thủ công**: Admin vào `/developer` bấm "Xác nhận đã nhận tiền" sau khi kiểm tra sao kê (app không có quyền đọc sao kê ngân hàng tự động).
- **Admin "tặng" gói cho học sinh**: vẫn thao tác y hệt mục 10 (đổi gói trực tiếp từ `/developer`), nhưng giờ dùng chung logic với cổng thanh toán thật nên **chỉ tặng đúng 1 tháng miễn phí**, không phải vĩnh viễn — hết tháng đó học sinh cần tự thanh toán tiếp (hoặc Admin tặng lại) như mọi tài khoản khác. Không áp dụng cho tài khoản Developer trở lên vì các vai trò đó đã luôn có Max vô điều kiện.
- Hộp thoại "Nâng cấp gói" tự hiện huy hiệu "🎁 Giảm 50% — còn N tháng ưu đãi" + giá gốc gạch ngang khi tài khoản còn đủ điều kiện; hết ưu đãi thì tự quay lại hiện giá gốc, không cần Admin can thiệp.

⚠️ Chưa có: gia hạn tự động trừ tiền định kỳ (app không lưu thông tin thẻ để làm việc đó — học sinh cần tự vào lại nâng cấp mỗi tháng), hoá đơn/biên lai điện tử, hoàn tiền.

## 15. Sửa lỗi giao diện (mới)

- **Nút "Báo lỗi" dưới câu trả lời AI bị vô hình**: nút vẫn nằm trong DOM và bấm được, nhưng CSS `.ai-msg-group:hover .msg-actions` yêu cầu nút phải là **con** của khung tin nhắn để hiện khi rê chuột — trong khi JS lại chèn nút bằng `wrapper.after(bar)`, tức là **anh em cùng cấp**, không phải con, nên `opacity` luôn bằng 0. Đã đổi sang bộ chọn anh em `.ai-msg-group:hover ~ .msg-actions` + thêm `:hover`/`:focus-within` riêng cho nút để cả di động (không có hover) và bàn phím đều bấm được.
- **Nút chuyển giao diện Sáng/Tối không đổi gì cả**: Tailwind được nhúng qua CDN (`cdn.tailwindcss.com`), mặc định chế độ tối theo `prefers-color-scheme` của hệ điều hành (`darkMode: 'media'`) — nghĩa là JS tự bật/tắt class `dark` trên thẻ `<html>` **không có tác dụng gì** với các lớp `dark:` nếu không khai báo lại. Đã thêm `<script>tailwind.config = { darkMode: 'class' }</script>` ngay sau mỗi lần nhúng CDN Tailwind (6 trang HTML trong `app.py`) để JS điều khiển được thật.
- **Avatar robot có hiệu ứng "đang suy nghĩ"**: một dải sáng quét dọc từ **dưới lên trên**, lặp lại — chạy nhanh & rõ trên avatar khi AI đang trả lời, chạy chậm & mờ liên tục ở logo robot trong sidebar để làm avatar chung của cả web.
- **Lỗi "Cannot operate on a closed database" do tự restart giữa lúc đang trả lời**: nguyên nhân khác với lỗi đã sửa ở mục 12 (đó là do `teardown_appcontext` đóng kết nối sớm; lỗi này là do Werkzeug **tự restart cả tiến trình**). Khi chạy bằng `python app.py` với `debug=True`, Werkzeug mặc định bật `use_reloader` — theo dõi thư mục dự án, hễ có file thay đổi là tự khởi động lại server. Vì `studymate.db` (SQLite) bị ghi mỗi khi có tin nhắn mới, nó cũng bị tính là "file thay đổi" → server tự restart ngay giữa lúc đang stream câu trả lời, kết nối SQLite của request đó bị đóng đột ngột. Đã thêm `use_reloader=False` vào `app.run(...)` ở cuối file để tắt cơ chế tự restart này (vẫn giữ `debug=True` để còn thấy traceback khi phát triển) — sửa xong code thì tự dừng (`Ctrl+C`) và chạy lại thủ công.

## 16. Báo lỗi câu trả lời, Bộ nhớ AI, Điểm thưởng & Chuỗi ngày học (đã có sẵn trong nhánh này)

- **Báo lỗi câu trả lời**: mỗi câu trả lời của AI có nút "Báo lỗi" (hiện khi rê chuột/chạm vào tin nhắn — xem lỗi hiển thị đã sửa ở mục 15), mở hộp thoại cho học sinh chọn lý do + ghi chú thêm, lưu vào `/developer` để Admin xem và đánh dấu đã xử lý.
- **Bộ nhớ AI**: AI tự trích và ghi nhớ vài thông tin học sinh nhắc tới trong lúc trò chuyện (vd: đang học lớp mấy) để cá nhân hoá câu trả lời sau này — có toast nhỏ báo "Đã ghi nhớ: ..." ngay khi xảy ra. Học sinh có thể xoá toàn bộ bộ nhớ này bất cứ lúc nào bằng nút **"Xoá bộ nhớ AI của tôi"** trong Cài đặt.
- **Điểm thưởng (XP) & chuỗi ngày học**: mỗi lượt hỏi AI thành công được cộng XP, tính streak theo ngày (múi giờ VN, không tính 2 lượt cùng ngày là 2 ngày streak) — hiện ở góc sidebar. Có 4 thành tựu mở khoá tự động: 🧠 Bài học đầu tiên, 🔥 Chuỗi 7 ngày, 🏆 Chuỗi 30 ngày, 📚 100 câu hỏi — báo bằng toast khi vừa đạt được.

## 17. Thẻ ghi nhớ (Flashcards) + Trò chơi luyện tập + biểu tượng PWA (mới)

Nút mới **"Thẻ ghi nhớ & Trò chơi"** ngay dưới "Đoạn chat mới" ở sidebar, mở ra 1 màn hình riêng:

- **Bộ thẻ ghi nhớ**: tạo trống rồi tự thêm từng thẻ (mặt trước/mặt sau), hoặc bấm **"Tạo bằng AI ✨"** — chỉ cần nhập 1 chủ đề (vd: "Từ vựng tiếng Anh Unit 5", "Hằng đẳng thức đáng nhớ"), AI tự soạn 4-12 thẻ chỉ với **1 lượt gọi API** (không streaming, parse JSON kết quả), lỗi định dạng thì báo rõ để thử lại chứ không hiện thẻ rác.
- **Chế độ Học**: lật từng thẻ (bấm vào thẻ để lật), tự đánh giá "Đã nhớ"/"Chưa nhớ" — dùng kiểu **Leitner đơn giản** (đúng thì tăng mức độ nhớ tối đa 5, sai thì về mức 1 để ưu tiên ôn lại sớm hơn ở lượt học sau). Mức độ nhớ hiện luôn trên từng thẻ trong danh sách (Lv1-Lv5).
- **Trò chơi "Lật thẻ ghi nhớ"** (Memory Match): xáo mặt trước/mặt sau của tối đa 8 cặp thẻ thành lưới, tìm đúng cặp khớp nhau — có đếm thời gian + số lượt lật, thưởng XP khi thắng (điểm thưởng cao hơn nếu nhanh & ít lượt lật sai).
- Cả 2 chế độ đều nối vào hệ Điểm thưởng (XP)/Streak/Thành tựu có sẵn (mục 16): thêm 2 thành tựu mới 🗂️ **Bộ thẻ đầu tiên** và 🎮 **Người chơi mới**.
- API liên quan: `GET/POST /api/decks`, `POST /api/decks/generate`, `GET/PATCH/DELETE /api/decks/<id>`, `POST /api/decks/<id>/cards`, `PATCH/DELETE /api/cards/<id>`, `POST /api/games/complete`.

**Biểu tượng ứng dụng (PWA)**: đã gắn 2 icon bạn gửi vào `static/icons/`, thêm route `/manifest.json` (đã sửa tên trong đó từ "StudyMate AI Pro" thành **"StudyMate AI"** cho đúng quy tắc đặt tên ở mục 10) và các thẻ `<link rel="manifest">`/`theme-color`/`apple-touch-icon` vào `<head>` — giờ bấm "Cài đặt ứng dụng" / "Thêm vào màn hình chính" trên điện thoại sẽ hiện đúng icon + tên bạn cung cấp thay vì icon mặc định của trình duyệt.

⚠️ Chưa có: xoá bớt/đổi icon qua giao diện Admin (hiện phải tự thay file trong `static/icons/`), quiz trắc nghiệm tự sinh từ bộ thẻ (khác với chế độ Học lật thẻ), chia sẻ bộ thẻ giữa các tài khoản.

## 18. Sửa lỗi hiển thị ký hiệu toán học (căn bậc hai...) — báo bởi học sinh (mới, quan trọng)

Học sinh **BlackadaNutella** báo lỗi qua nút "Báo lỗi": câu trả lời hiện nguyên chữ `\( \sqrt{a} \)` thay vì ký hiệu căn bậc hai đẹp — "không đọc được".

**Nguyên nhân**: hệ render công thức (KaTeX) vốn hoạt động đúng, nhưng không có gì đảm bảo AI luôn viết LaTeX **cân bằng dấu ngoặc** hoặc không lỡ **chép lại y nguyên** ký hiệu bị lỗi/không rõ ràng mà học sinh gõ vào — khi 1 công thức bị lệch dấu, KaTeX không render được, để lộ nguyên cú pháp LaTeX thô ra màn hình.

**Đã sửa 2 lớp**:
1. **System prompt** (mọi chế độ, kể cả AI Tutor tuỳ chỉnh): thêm quy tắc bắt buộc tự kiểm tra số dấu mở = số dấu đóng trước khi trả lời, và **cấm chép lại y nguyên** ký hiệu toán học bị lỗi từ học sinh — phải tự hiểu ý rồi viết lại bằng LaTeX chuẩn hoặc bằng lời.
2. **Lưới an toàn phía trình duyệt** (`fallbackReadableMath()`, hàm mới): sau mỗi lần KaTeX cố render, quét lại phần tử — nếu vẫn còn sót cú pháp LaTeX thô (KaTeX không render được vì lý do gì đó), tự thay bằng ký hiệu Unicode dễ đọc (`\sqrt{a}` → `√(a)`, `\frac{a}{b}` → `(a)/(b)`, `\times`→`×`, `\pi`→`π`, `\le`→`≤`...) — học sinh **không bao giờ** phải nhìn thấy cú pháp LaTeX thô nữa, kể cả trong trường hợp xấu nhất. Đã kiểm thử kỹ bằng Node.js với đúng câu trong báo cáo lỗi, hoạt động chính xác.

## 19. Sổ lỗi sai (Mistake Book) (mới)

Theo đúng thứ tự ưu tiên bạn đề ra (AI Memory + Mistake Book làm trước tiên):

- Dưới mỗi câu trả lời AI, cạnh nút "Báo lỗi" có thêm nút **"Lưu vào Sổ lỗi sai"** — học sinh tự mô tả ngắn gọn lỗi mình vừa mắc (vd: "Chuyển vế quên đổi dấu"), kèm môn học.
- **Lỗi lặp lại** (cùng môn + cùng mô tả, đã chuẩn hoá hoa/thường) không tạo dòng mới — chỉ tăng số đếm, hiển thị đúng kiểu `Chuyển vế sai dấu ×3` như mô tả của bạn.
- Xem trong tab **"Sổ lỗi sai"** (cạnh tab "Thẻ ghi nhớ", cùng màn hình): nhóm theo môn học, lỗi lặp nhiều xếp lên đầu.
- Nút **"Ôn lại ngay"** trên mỗi lỗi: đóng Sổ lỗi sai, mở đoạn chat mới, tự chọn sẵn môn học + Chế độ "Luyện tập", điền sẵn câu hỏi nhờ AI ra 3 bài đúng dạng lỗi đó — biến việc "biết mình sai gì" thành hành động luyện tập ngay, không chỉ ghi chép suông.
- Đánh dấu "Đã khắc phục" khi không còn mắc lỗi đó nữa (ẩn khỏi danh sách chính, có thể mở lại).
- Nối vào hệ Điểm thưởng: +5 XP mỗi lần ghi nhận (kể cả lặp lại — tự nhận ra lỗi cũng đáng khích lệ), thành tựu mới 📕 **Tự nhận ra lỗi**.
- API: `GET/POST /api/mistakes`, `PATCH/DELETE /api/mistakes/<id>`.

⚠️ Đây là Sổ lỗi sai dựa trên **học sinh tự mô tả**, không phải AI tự động phát hiện và phân loại lỗi (việc đó cần AI phân tích riêng từng câu trả lời — có thể làm ở bản sau nếu bạn muốn, nhưng sẽ tốn thêm 1 lượt gọi API mỗi câu trả lời ở chế độ "Kiểm tra bài làm").

## Về roadmap dài hạn bạn chia sẻ

Đã đọc kỹ phần định hướng Phase 1-4 và danh sách 5 tính năng ưu tiên. Đồng ý hướng "AI hiểu người học, không chỉ hiểu câu hỏi" là lợi thế cạnh tranh hợp lý so với chép lại ChatGPT/Claude. Bộ nhớ AI (đã có) + Sổ lỗi sai (mục 19) là bước khởi đầu đúng hướng cho Phase 2. Quiz Generator, Study Plan, và AI Tutor Store (marketplace công khai cho Custom Tutor) vẫn **chưa làm** — mỗi cái là 1 hệ thống riêng khá lớn (Quiz Generator cần chấm điểm tự động + phân tích điểm yếu; Study Plan cần lịch trình đa ngày + tự điều chỉnh; AI Tutor Store cần thêm khái niệm "publish công khai" lên trên hệ Custom Tutor hiện chỉ có ở mức cá nhân/developer). Nói cụ thể bạn muốn làm cái nào tiếp theo, mình sẽ tập trung làm cho xong 1 cái thay vì làm dở cả 3.

## 20. Sửa lỗi crash "no such column: resolved_by" + rà soát toàn bộ schema database (mới, quan trọng)

Bạn báo lỗi crash thật khi bấm "Đánh dấu đã xử lý" ở `/developer/issues/2/resolve`: `sqlite3.OperationalError: no such column: resolved_by`.

**Nguyên nhân**: bảng `issue_reports` được tạo lần đầu (ở máy bạn) từ một phiên bản code CŨ, lúc đó cột `resolved_by` chưa tồn tại. Sau này code có thêm cột đó vào câu lệnh `CREATE TABLE IF NOT EXISTS` — nhưng vì bảng ĐÃ tồn tại rồi nên lệnh đó chỉ là no-op, không tự thêm cột mới vào bảng cũ. Đây là kiểu lỗi "schema drift" kinh điển khi phát triển thêm tính năng cho 1 database SQLite đã có dữ liệu.

**Đã sửa tận gốc, không chỉ vá 1 cột**: thêm hàm `ensure_columns()` — tự dò và thêm MỌI cột còn thiếu cho MỌI bảng, mỗi lần khởi động server, an toàn để chạy lại nhiều lần. Đã áp dụng cho toàn bộ 18 bảng trong app, không riêng `issue_reports`. Đã kiểm thử bằng cách **giả lập chính xác** database cũ của bạn (tạo bảng `issue_reports` thiếu cột `resolved_by`) rồi xác nhận: khởi động app lên → cột tự xuất hiện → bấm "Đánh dấu đã xử lý" chạy bình thường, không còn crash.

**Đã rà soát toàn bộ 67 route** trong app (kiểm thử tự động, không phải chỉ đọc code) — không phát hiện lỗi crash nào khác.

## 21. Sửa ký hiệu toán học lần 2 — đơn giản hoá lời dặn AI (mới)

Bạn gửi tiếp ảnh cho thấy vẫn còn vấn đề: câu trả lời có nhiều dấu ngoặc thừa kiểu `( √(a) )`, và 1 từ tiếng Anh lạc "monospaced" xuất hiện giữa câu trả lời tiếng Việt.

Lưới an toàn phía trình duyệt (mục 18) hoạt động đúng — đó là lý do không còn thấy cú pháp `\( \)` thô nữa. Nhưng lời dặn (system prompt) mình thêm ở mục 18 hơi dài dòng, giải thích quá chi tiết "nếu sai thì sẽ hiện lỗi gì" — nghi vấn là AI bắt chước phong cách dài dòng/cẩn trọng quá mức đó, dẫn tới thừa ngoặc + lạc từ. Đã **rút gọn đáng kể** lời dặn (còn 2 câu ngắn, không mô tả chi tiết trường hợp lỗi) ở cả 2 nơi (chế độ thường + AI Tutor tuỳ chỉnh).

⚠️ Thành thật lưu ý: đây là hành vi của model AI thật (xAI/Grok), mình không có cách nào gọi thử model thật từ môi trường đang code để kiểm chứng 100% trước khi giao cho bạn — khác với các lỗi code (Python/SQL/JS) mình LUÔN chạy thử và xác nhận trước khi báo đã sửa. Bạn thử lại và cho mình biết còn hiện tượng này không, nếu còn thì mình sẽ rút gọn lời dặn hơn nữa hoặc bỏ hẳn đoạn đó, chỉ dựa hoàn toàn vào lưới an toàn phía trình duyệt (vốn đã đảm bảo học sinh không bao giờ thấy cú pháp `\( \)` thô, dù có thừa ngoặc thì cũng không nghiêm trọng bằng).

## 22. Hiệu ứng ngọn lửa khi đạt mốc streak (mới)

Đúng như yêu cầu: khi số ngày học liên tục (streak) chạm mốc **3, 10, 30, 100, 200, 300, 500, 1000**, một hiệu ứng ngọn lửa 🔥 bùng lên giữa màn hình (không chỉ đổi số nhỏ ở sidebar), tự biến mất sau ~2.7 giây.

**"Ngọn lửa ngày càng đậm"**: mốc càng cao thì lửa càng "nặng đô" — nhiều lớp 🔥🔥🔥 hơn, kích thước lớn hơn, glow (quầng sáng) toả rộng và đậm hơn, màu ngả dần từ vàng (mốc 3) → cam (mốc 10-200) → đỏ (mốc 300) → tím-đỏ ở mốc 500-1000 (kèm 👑 cho 2 mốc huyền thoại này).

Chỉ bắn hiệu ứng khi streak **thực sự tăng lên đúng mốc trong phiên đang dùng** (không bắn lại mỗi lần tải trang nếu streak hiện tại tình cờ đang ở mốc từ hôm trước).

## 23. Quiz Generator + Study Plan — hoàn thành Phase 1 (mới)

Bạn gửi bản đặc tả sản phẩm rất lớn (41 mục — Memory, Mistake Book, Quiz, Study Plan, Teacher Mode, AI Tutor Store, Voice Mode, Screen Capture, Command Palette...). Đây là tầm nhìn nhiều năm cho 1 sản phẩm, không thể làm hết trong 1 lượt mà vẫn đảm bảo chất lượng — đặc biệt khi chính bản đặc tả đó yêu cầu "không tạo nút giả không hoạt động". Vì vậy mình tập trung hoàn thành nốt **Phase 1** theo đúng roadmap bạn tự đề ra: AI Memory ✅, Mistake Book ✅, Achievement + XP ✅ (đã có từ trước) — còn thiếu **Quiz Generator** và **Study Plan**, nay đã làm xong.

Cả 2 tính năng đều xuất hiện trong overlay "Thẻ ghi nhớ & Trò chơi" (đổi thành 4 tab: Thẻ ghi nhớ | Sổ lỗi sai | Quiz | Kế hoạch ôn tập).

### 📝 Quiz Generator
- AI tạo đề chỉ từ 1 chủ đề (hoặc dựa trên nội dung 1 đoạn chat có sẵn), chọn độ khó (Dễ/Trung bình/Khó/Nâng cao) và số câu.
- 3 dạng câu hỏi: **trắc nghiệm, đúng/sai, điền khuyết** — cố tình CHỈ chọn 3 dạng này vì chấm được tự động, chính xác 100%, không tốn thêm lượt gọi AI nào lúc chấm bài (so khớp đáp án đã chuẩn hoá). Dạng tự luận/ghép nối cần AI chấm chủ quan nên chưa hỗ trợ — xem phần "Chưa làm".
- Làm bài từng câu, nộp bài ra ngay: điểm số, % đúng, thời gian, **chủ đề còn yếu** (dựa trên câu sai, gom theo "topic" AI tự gắn cho từng câu), xem lại từng câu kèm giải thích.
- **Nối liền hệ sinh thái có sẵn**: câu trả lời sai tự động lưu vào Sổ lỗi sai (dùng chung logic gộp trùng lặp ở mục 19); hoàn thành quiz cộng XP, đạt 100% mở khoá thành tựu 💯 **Điểm tuyệt đối**.

### 🎯 Study Plan
- Nhập mục tiêu (vd: "Ôn thi Toán 8 trong 14 ngày") — AI chia thành việc cho từng ngày, ngày cuối luôn là ôn tập tổng hợp.
- Mỗi việc: **Hoàn thành** (✓, cộng XP), **Bỏ qua**, hoặc **Hỏi AI** (mở đoạn chat mới hỏi thẳng về chủ đề hôm đó — tái dùng đúng cơ chế "Ôn lại ngay" của Sổ lỗi sai).
- **Tự động phát hiện trễ tiến độ**: nếu có việc ở ngày đã qua mà vẫn "Chưa làm", nút **"Sắp xếp lại"** hiện ra — gọi AI phân bổ lại các việc CÒN THIẾU vào số ngày còn lại, giữ nguyên các việc đã hoàn thành (không mất tiến độ đã có). Đây chính là phần "tự điều chỉnh kế hoạch theo tiến độ" trong đặc tả của bạn.
- Hoàn thành trọn kế hoạch mở khoá thành tựu 🏆 **Về đích**.

Đã kiểm thử đầy đủ backend (sinh đề/kế hoạch, chấm điểm, gộp lỗi sai trùng lặp, sắp xếp lại kế hoạch giữ tiến độ) và toàn bộ giao diện render không lỗi, cộng với rà soát lại lần nữa toàn bộ 67+ route để đảm bảo không phát sinh lỗi mới.

## 24. Đăng nhập khách + Avatar + Quản lý tài khoản + Công cụ kiểm thử cho Developer (mới)

### 👤 Đăng nhập khách ("dùng thử ngay, không cần đăng ký")
- Nút mới ở trang đăng nhập, tạo 1 tài khoản khách THẬT trong DB (username dạng `khach_xxxxxxxx`, không có mật khẩu) — dùng lại toàn bộ hạ tầng sẵn có (chat, XP/streak, thẻ ghi nhớ, sổ lỗi sai...) mà không cần viết thêm code riêng cho "chế độ ẩn danh".
- Đánh dấu rõ bằng nhãn **"KHÁCH"** cạnh tên trong sidebar.
- **Tạo tài khoản chính thức bất cứ lúc nào** (mục "Tài khoản" trong Cài đặt) — chỉ cần đặt username + mật khẩu, dữ liệu đã dùng thử (đoạn chat, XP, thẻ ghi nhớ...) được **giữ nguyên 100%** vì thao tác này cập nhật thẳng lên dòng tài khoản hiện tại, không tạo tài khoản mới.
- Admin bật/tắt được từ `/developer` (mặc định BẬT, không cần cấu hình .env).
- ⚠️ Đánh đổi tất yếu của "khách": không có mật khẩu nên **mất tài khoản nếu xoá cookie trình duyệt** trước khi tạo tài khoản chính thức — đã ghi chú rõ ngay dưới nút đăng nhập khách.

### 🎨 Avatar + Quản lý tài khoản (mọi tài khoản đều dùng được)
- 16 avatar hình emoji (🦊🐱🐼🦁🐸🐧🦉🐢🐬🦄🐙🦋🐨🐯🐰🐳) trên nền gradient màu riêng — chọn trong Cài đặt, lưu ngay, hiện luôn ở sidebar. Chưa chọn thì vẫn về mặc định chữ cái đầu tên như trước (không đổi giao diện với ai chưa dùng tính năng này).
- **Đổi mật khẩu** ngay trong Cài đặt (xác thực đúng mật khẩu hiện tại trước khi đổi) — mục còn thiếu đã ghi chú ở các bản trước, nay bổ sung.
- Tài khoản đăng nhập Google: hiện ghi chú "không cần mật khẩu ở đây" thay vì hiện form đổi mật khẩu không dùng được.

### 🧪 Công cụ kiểm thử (Sandbox) — dành cho Developer
Panel mới trong `/developer`, mục đích: **test tính năng nhanh mà không cần chờ dữ liệu thật** (vd: không cần đợi dùng app 500 ngày liên tục mới thấy hiệu ứng streak mốc 500):
- **Chỉnh XP / streak trực tiếp** cho bất kỳ tài khoản nào (kể cả chính mình) — gán thẳng số XP, streak hiện tại, streak dài nhất, áp dụng ngay lập tức. Đã kiểm thử: tài khoản mục tiêu load lại trang thấy đúng cấp độ/XP mới ngay, và **xác nhận tài khoản KHÔNG PHẢI admin bị chặn** khi cố gọi thẳng route này (bảo mật đúng như các route admin khác).
- **Xem trước hiệu ứng ngọn lửa streak**: 8 nút bấm (mốc 3/10/30/100/200/300/500/1000), mỗi nút mở trang chat ở tab mới và tự bắn hiệu ứng ngay khi tải xong — CHỈ hiển thị hình ảnh, không đụng tới dữ liệu XP/streak thật của ai cả (an toàn để bấm thử thoải mái).

⚠️ "Chỉnh các chế độ" trong yêu cầu gốc của bạn hơi mơ hồ (không rõ ý là Chế độ suy nghĩ, Chế độ học tập, hay thứ khác) — hiện mình mới làm phần XP/streak (rõ nghĩa nhất, khớp với "để test... hiệu ứng"). Nếu ý bạn là 1 loại "chế độ" cụ thể khác, nói rõ hơn để mình bổ sung đúng.

## 25. Về yêu cầu "Desktop Companion" (Alt+Space, system tray, chụp màn hình OS) — cần đọc trước khi dùng

Bạn gửi 1 bản đặc tả rất chi tiết cho 1 **ứng dụng desktop Windows thật** (Tauri/Electron + React + TypeScript) với phím tắt toàn hệ điều hành, system tray, chụp màn hình cấp OS, theo dõi clipboard... Cần nói thẳng trước khi bạn kỳ vọng nhầm:

**Phần này KHÔNG THỂ xây dựng như 1 tính năng mở rộng của `app.py` (Flask web app hiện tại)**, vì lý do kỹ thuật thật sự, không phải mình ngại làm:

1. **Khác hẳn nền tảng**: `ALT+SPACE` hoạt động được ngay cả khi đang chơi Minecraft hay gõ Word — đó là hotkey **cấp hệ điều hành**. Trình duyệt (và app Flask chạy trong trình duyệt) **không có quyền** đăng ký hotkey toàn hệ thống — đây là giới hạn bảo mật cố ý của mọi trình duyệt, không phải thứ có thể "code thêm" để vượt qua. Muốn có `ALT+SPACE` thật, bắt buộc phải là 1 ứng dụng desktop biên dịch riêng (native), viết bằng Tauri (Rust) hoặc Electron (Node.js) — 2 công nghệ này **không chạy được trong `app.py`**, cần 1 dự án, 1 repo, 1 quy trình build hoàn toàn khác.
2. **Mình không kiểm thử được**: Toàn bộ phần còn lại của cuộc trò chuyện này, mọi tính năng mình giao đều đã chạy thử thật (test tự động, giả lập request, kiểm tra kết quả) trước khi gửi cho bạn — đó là lý do bạn có thể tin các tính năng đó hoạt động đúng. Với 1 ứng dụng Windows thật, mình **không có máy Windows, không có Rust/Node toolchain, không cài đặt/chạy thử được** — nếu mình viết code Tauri/Electron rồi gửi luôn mà không chạy thử, rất có thể sẽ có lỗi mà mình không phát hiện ra được, khác hẳn cách làm việc nghiêm túc mình đã giữ suốt từ đầu.
3. **Không có đường "cài đặt" qua chat**: Kể cả code đúng 100%, đây vẫn là 1 ứng dụng cần build ra file `.exe`/`.msi` rồi cài vào Windows — không phải thứ có thể "gửi qua chat" như file `app.py` được.

**Vì vậy mình đã làm phần mà chính bản đặc tả của bạn cũng ghi rõ là hướng đi đúng cho người dùng web** ("Fallback for Web Users" trong đặc tả — dùng `CTRL+K` cho command palette trong trình duyệt thay vì giả vờ đó là phím tắt toàn hệ thống):

### ⌨️ Command Palette / Web Quick Launcher (`Ctrl/⌘ + K`) — đã làm, đã test
- Bấm `Ctrl/⌘ + K` ở bất kỳ đâu trong app → mở bảng lệnh nhanh kiểu Raycast/Linear ngay giữa màn hình.
- Gõ để lọc lệnh: Đoạn chat mới, Tạo Quiz bằng AI, Tạo bộ thẻ ghi nhớ bằng AI, Tạo kế hoạch ôn tập, Sổ lỗi sai, Cài đặt, Nâng cấp gói, Trợ giúp, (Trang Developer nếu có quyền), Đăng xuất.
- **Gõ thẳng câu hỏi rồi Enter** → mở đoạn chat mới và hỏi AI ngay lập tức — đúng tinh thần "Ask AI" trong đặc tả gốc, chỉ khác là mở trong trang thay vì cửa sổ nổi trên desktop.
- Điều hướng bằng phím mũi tên lên/xuống + Enter để chọn, ESC để đóng.
- Đây cũng chính là mục **"⌨️ Command Palette"** ở Phase 4 trong roadmap bạn tự đề ra — coi như đã hoàn thành mục đó.

⚠️ Đổi 1 hành vi cũ: `Ctrl+K` trước đây tạo đoạn chat mới ngay lập tức; giờ mở bảng lệnh trước (giống Linear/Notion) — "Đoạn chat mới" vẫn là lựa chọn đầu tiên, chỉ cần bấm Enter thêm 1 lần.

### Nếu bạn THỰC SỰ muốn app desktop Windows thật
Đây sẽ là 1 dự án tách biệt hoàn toàn khỏi `app.py`. Vài điều thật sự cần biết trước khi bắt đầu (không phải code, chỉ là định hướng kỹ thuật trung thực):
- **Tauri** (khuyến nghị, đúng như đặc tả gốc đề xuất) — nhẹ hơn Electron vì dùng WebView có sẵn của hệ điều hành thay vì đóng gói cả Chromium. Cần cài Rust toolchain.
- Đăng ký global hotkey: crate `tauri-plugin-global-shortcut`.
- System tray: crate `tray-icon` (hoặc plugin tray tích hợp sẵn trong Tauri 2.x).
- Chụp vùng màn hình: crate `screenshots` hoặc `xcap`, kèm xin quyền hệ điều hành (Windows sẽ tự hỏi quyền Chụp màn hình nếu chạy trên Windows 10+ tuỳ cấu hình).
- App desktop này gọi vào **CHÍNH các API `/api/chat`, `/api/decks/generate`, `/api/quizzes/generate`... mà `app.py` đã có sẵn** — không cần viết lại logic AI, chỉ cần app desktop biết đăng nhập (lưu session/cookie hoặc đổi sang API key — xem `/api/keys` đã có sẵn cho Developer) rồi gọi HTTP tới server Flask này.
- Ước lượng thực tế: đây là 1 dự án vài tuần cho 1 người biết Rust/Tauri, không phải vài giờ.

Nếu bạn muốn, mình có thể bắt đầu 1 cuộc trò chuyện RIÊNG chuyên về dự án Tauri này (không lẫn vào `app.py`), viết code từng phần và giải thích rõ phần nào mình **chưa** chạy thử được (vì không có Windows) để bạn tự kiểm tra khi build máy thật — miễn là hiểu rõ từ đầu đây là 1 dự án khác, tốc độ và độ tin cậy sẽ khác hẳn so với những gì mình giao trong `app.py` suốt từ đầu tới giờ.

## 26. Trò chơi học tập + Developer Lab (mới) — đã chọn lọc kỹ từ bản đặc tả 40 mục

Bạn gửi 1 bản đặc tả cực lớn: Design Studio đổi giao diện toàn diện (background/theme/chat bubble/nút tuỳ chỉnh), Developer Lab đầy đủ (Experiments/Sandbox/Game Lab/Feature Flags), và cả 1 nền tảng game giáo dục (Flappy Study, Snake Quiz, Memory Match, Quick Math...) kèm hệ thống Developer tự tạo/publish/kiểm duyệt game. Đây thực chất là đặc tả cho vài dự án riêng biệt. Đã chọn ra phần **làm được TRỌN VẸN, kiểm thử được đầy đủ**, đúng tinh thần "không để nút bấm giả" mà chính bản đặc tả của bạn yêu cầu:

### ⚡ Đố Vui Tính Nhanh (Quick Math) — trò chơi mới, đã test kỹ
- 60 giây, trả lời phép cộng/trừ/nhân/chia trắc nghiệm 4 đáp án, 3 mức độ khó (Dễ/Trung bình/Khó).
- Đã **kiểm thử 60.000 câu hỏi được sinh ngẫu nhiên** (chạy độc lập bằng Node.js) để đảm bảo: phép chia luôn chia hết (không có số dư khó chịu), phép trừ không bao giờ ra âm, đáp án đúng luôn nằm trong 4 lựa chọn, không có vòng lặp vô hạn khi sinh đáp án nhiễu — mức độ chắc chắn này thường chỉ có được khi test trên trình duyệt thật, ở đây làm được vì logic sinh câu hỏi thuần JS, không phụ thuộc DOM/canvas.
- **"Post-Game Learning Report" (mục 23-24 trong đặc tả)**: sau ván chơi, tự động thống kê em hay sai PHÉP TÍNH nào (cộng/trừ/nhân/chia) — không cần gọi AI để biết điều này, chỉ đếm trực tiếp từ ván chơi. Phép tính hay sai được **tự động lưu vào Sổ lỗi sai** (dùng lại đúng cơ chế gộp trùng lặp đã có), gộp ĐÚNG kiểu cộng dồn qua nhiều ván chơi khác nhau (chơi lại vào hôm sau vẫn sai phép nhân → tăng số đếm, không tạo dòng mới) — đã kiểm thử kỹ điều này.
- Có XP + 2 thành tựu mới: ⚡ **Tia chớp** (combo 10 câu đúng liên tiếp), 🎯 **Không sai một câu** (100% chính xác, từ 10 câu trở lên).
- Đã bỏ qua bước "AI tạo 1 quiz trắc nghiệm sau khi chơi xong" mà đặc tả yêu cầu — vì bản thân ván Tính Nhanh ĐÃ LÀ một chuỗi câu hỏi rồi, thêm 1 lớp quiz AI riêng sau đó sẽ dư thừa, không có giá trị học tập thêm. Thay vào đó tập trung vào phần thật sự hữu ích: xác định điểm yếu + tự lưu vào Sổ lỗi sai.
- Xem trong tab **"Trò chơi"** mới (cạnh Quiz, Kế hoạch ôn tập) — cùng chỗ với Lật thẻ ghi nhớ (đã có từ trước), có bảng điểm cao nhất riêng cho từng trò.

### 🧪 Developer Lab (`/developer/lab`) — Feature Flags
- Trang mới, Developer trở lên vào được (không cần tới Admin).
- Tạo/bật/tắt "cờ tính năng" (feature flag) theo 3 cấp: **off** (tắt hẳn), **internal** (chỉ Developer trở lên thấy — tự test trước khi công khai), **public** (bật cho mọi người) — không cần sửa code hay khởi động lại server.
- Đã kiểm thử đủ 3 cấp + trường hợp flag chưa tồn tại (mặc định an toàn = tắt) + chặn tài khoản thường truy cập trang này.
- ⚠️ Hiện CHƯA có tính năng thử nghiệm cụ thể nào trong app đang dùng flag này (không có gì đang "làm dở" cần giấu bớt) — đây là hạ tầng chuẩn bị sẵn cho tính năng thử nghiệm sau này, không phải tính năng có sẵn output ngay lập tức. Trang Dev Lab cũng gộp luôn số liệu tổng hợp 2 trò chơi (tổng lượt chơi, điểm trung bình, độ chính xác trung bình).

### Đã CHỦ ĐỘNG bỏ qua (và lý do cụ thể)
- **Flappy Study / Snake Quiz**: cần game loop canvas + vật lý + va chạm — loại code mình không thể "nhìn thấy chạy" để xác nhận mượt/đúng như Quick Math (vốn chỉ là DOM + logic thuần, test được bằng code). Làm ẩu 1 game canvas lỗi còn tệ hơn không làm.
- **Design Studio đổi toàn bộ giao diện** (background upload, đổi màu chủ đạo toàn app, tuỳ chỉnh bong bóng chat...): app hiện dùng class màu Tailwind viết cứng (`bg-blue-600`...) rải khắp hàng nghìn dòng, không dùng biến CSS theo màu — muốn đổi màu chủ đạo TOÀN BỘ giao diện một cách nhất quán cần thay thế có hệ thống rất nhiều chỗ, rủi ro làm vỡ giao diện ở đâu đó mà không kiểm hết được trong 1 lượt. Đây là việc làm được nhưng cần 1 đợt riêng, làm cẩn thận từng phần.
- **Developer Game Creator + publish + kiểm duyệt Admin**: là 1 hệ thống lớn tương đương với việc xây 1 "App Store" mini cho game — ngoài phạm vi hợp lý của 1 lượt cập nhật.
- **Experiments/versioning, API Test riêng** (đã có Playground), **Deployments**: trùng lặp hoặc không áp dụng được với kiến trúc Flask hiện tại.

## 27. Dữ liệu người dùng có mất khi "lên live" không? (câu trả lời ngắn: KHÔNG, với 1 điều kiện)

**Không mất — với điều kiện duy nhất: bạn phải mang theo file `studymate.db` khi deploy, không được để nó tạo mới từ đầu.**

Đã kiểm chứng trực tiếp (không chỉ đọc code): tạo 1 tài khoản, sau đó **giả lập việc "lên live"** bằng cách chạy lại toàn bộ `init_db()` trên đúng file database đó (đúng những gì xảy ra mỗi lần khởi động server) — tài khoản vẫn còn nguyên. Lý do: toàn bộ hệ thống migration (`ensure_columns()`, xem mục 20) chỉ THÊM cột còn thiếu, không bao giờ xoá bảng hay xoá dữ liệu. Đã rà lại toàn bộ code — không có `DROP TABLE` nào, chỗ duy nhất có `DELETE FROM users` là thao tác Admin xoá 1 tài khoản CỤ THỂ (có điều kiện `WHERE id = ?`), không phải xoá sạch.

**Việc bạn cần tự làm khi deploy thật** (đây là phần vận hành, không phải lỗi code):
- Copy file `studymate.db` (đang có ở máy bạn, cùng thư mục `app.py`) lên server, đặt CÙNG thư mục với `app.py` trên server — đừng để server tự tạo file `studymate.db` MỚI (rỗng).
- Nếu dùng Git để deploy: thêm `studymate.db` vào `.gitignore` (đừng để git ghi đè database production bằng database cũ hơn mỗi lần deploy) — copy file database sang server BẰNG TAY (hoặc dùng volume/mount riêng nếu deploy bằng Docker), tách biệt hẳn khỏi quy trình deploy code.
- **Nên sao lưu định kỳ**: `cp studymate.db studymate.db.backup-$(date +%Y%m%d)` trước mỗi lần deploy — phòng trường hợp thao tác deploy có sai sót gì đó ở phía hạ tầng (ngoài tầm kiểm soát của code Python).

## 28. StudyMate Lab — nâng cấp Feature Flags thành nền tảng thử nghiệm (mới)

Bạn gửi tiếp 1 bản đặc tả 51 mục cho "StudyMate Lab" — 1 nền tảng feature-flag cấp doanh nghiệp đầy đủ (Feature Registry, A/B Testing, Health Monitoring tự động, Release Approval Pipeline nhiều môi trường, Game Lab, UI Lab...). Đã **nâng cấp thật** trang Feature Flags có sẵn (không thay thế, đúng yêu cầu "extend, not replace") với phần làm được TRỌN VẸN và **kiểm chứng được bằng số liệu thống kê thật**, không phải chỉ đọc code rồi tin là đúng:

### Đã nâng cấp
- **Feature Registry đầy đủ hơn**: mỗi tính năng giờ có tên hiển thị, danh mục (games/ai/ui/learning/teacher/developer/other), phiên bản, người tạo, môi trường (nhãn thông tin), ngày hết hạn — không còn là 1 flag on/off trơn.
- **4 trạng thái → 5 trạng thái**: thêm **beta** (rollout theo %) và **archived** (tắt nhưng giữ lại lịch sử, khác với xoá hẳn).
- **Rollout theo % có kiểm chứng thống kê thật**: dùng hash ổn định (SHA-256) để 1 tài khoản LUÔN nhận cùng 1 kết quả bật/tắt cho cùng 1 flag — đã test với **10.000 tài khoản giả lập**, xác nhận tỉ lệ % thực tế lệch chưa tới 0.6% so với con số cấu hình (1%, 5%, 10%...100%), và xác nhận tính ổn định (gọi lại 100 lần vẫn ra cùng kết quả).
- **Phụ thuộc giữa các tính năng (dependencies)**: 1 tính năng có thể khai "phụ thuộc" vào tính năng khác — nếu phụ thuộc đó CHƯA bật cho đúng người dùng này thì tính năng chính cũng không bật được, dù trạng thái riêng của nó là gì. Đã kiểm thử đúng kịch bản trong đặc tả: bật Snake Quiz = public nhưng Quiz Engine (phụ thuộc) vẫn internal → học sinh KHÔNG thấy Snake Quiz; publish nốt Quiz Engine → học sinh thấy ngay.
- **Trang chi tiết từng tính năng** (`/developer/lab/features/<key>`): cấu hình đầy đủ + nhật ký thay đổi riêng của tính năng đó (lọc từ audit log có sẵn) + danh sách phụ thuộc kèm trạng thái.
- **Kill switch có xác nhận**: chuyển 1 tính năng ĐANG public về off sẽ hiện hộp thoại xác nhận (JS `confirm()`) — đã test: bấm "off" chặn truy cập ngay lập tức, kể cả với chính Developer.
- **Luật an toàn cốt lõi ("never auto-public")**: tính năng mới đăng ký LUÔN bắt đầu ở `internal`, không bao giờ tự động public — đã viết test xác nhận đúng luật này.
- **Tìm kiếm + lọc** theo tên/key/người tạo/trạng thái/danh mục.
- **Xoá hẳn** (khác "archived") — nâng cấp lên yêu cầu quyền Admin trở lên (không phải Developer thường) để tránh lỡ tay xoá tính năng đang chạy thật — đã kiểm thử: tài khoản Developer thường cố xoá bị chặn, dữ liệu vẫn còn nguyên.

### Đã CHỦ ĐỘNG bỏ qua (và lý do cụ thể)
- **Health Monitoring tự động + cảnh báo lỗi (mục 14-15)**: app hiện KHÔNG có hệ thống theo dõi lỗi/độ trễ theo TỪNG tính năng riêng lẻ — nếu hiển thị "🟢 Healthy, Error Rate 0.02%" mà không có số liệu thật đứng sau, đó chính là loại "nút giả" mà cả 2 bản đặc tả của bạn đều cấm. Cần 1 hệ thống logging theo dõi lỗi per-feature thật trước, việc này nằm ngoài phạm vi hợp lý của 1 lượt cập nhật.
- **A/B Testing / Experiments với biến thể + đo lường (mục 12-13)**: cần thêm 1 tầng gán biến thể + thu thập chỉ số riêng — có thể làm ở đợt sau nếu bạn có 1 thử nghiệm A/B cụ thể muốn chạy.
- **Nhiều môi trường triển khai thật (Dev/Sandbox/Staging/Production) — mục 6, 18-19**: app hiện chạy trên **1 server duy nhất** — "environment" trong bản nâng cấp chỉ là NHÃN THÔNG TIN (ghi chú), không đại diện cho hạ tầng tách biệt thật. Muốn có Staging/Production tách biệt thật cần 2 server + 2 database riêng — đây là quyết định hạ tầng/vận hành, không phải thứ code tự tạo ra được.
- **Release Approval Pipeline** (Developer request → Admin approve): đã có sẵn phân quyền Developer/Admin cho việc đổi trạng thái, nhưng chưa có bước "yêu cầu duyệt" riêng biệt — có thể thêm nếu bạn thấy cần thiết.
- **Game Lab / UI Lab / AI Playground như trang riêng**: đã có AI Playground (`/api/playground`, từ trước) và 2 game thật (Quick Math, Memory Match) — không xây thêm trang "tạo game mới"/"tạo UI mới" cho Developer vì đó là việc tương đương xây 1 công cụ no-code, quy mô hoàn toàn khác.
- **CI/CD Integration**: đúng như đặc tả của bạn tự ghi rõ ("giữ 2 hệ thống tách biệt") — app không có pipeline CI/CD nào để tích hợp, không có gì để làm ở mục này.

## 29. Đa ngôn ngữ (i18n) — mở rộng thật, không chỉ 9 nhãn như trước (mới)

Bạn gửi ảnh so sánh giao diện tiếng Việt/tiếng Anh và muốn khi đổi ngôn ngữ, MỌI THỨ đổi theo như vậy. Kiểm tra lại thì hệ thống `applyLanguage()` có sẵn từ trước CHỈ có 9 nhãn (Đoạn chat mới, Cài đặt, Đăng xuất...) — ảnh tiếng Anh bạn gửi (thấy cả "Mathematics", "Easy-to-Understand Explanation"...) nhiều khả năng đến từ tính năng dịch của trình duyệt/hệ điều hành, KHÔNG phải từ bộ chuyển ngôn ngữ của app. Đã mở rộng thật:

- Môn học + Chế độ (cả ở thanh trên cùng lẫn trong Cài đặt), 4 Chế độ suy nghĩ (tên + mô tả), placeholder ô nhập câu hỏi, dòng miễn trừ trách nhiệm cuối trang, nhãn "ngày"/"Cấp" ở khung XP, nút "Thẻ ghi nhớ & Trò chơi", trạng thái rỗng "Chưa có đoạn chat nào", toàn bộ mục Cài đặt (Giao diện/Sáng-Tối-Hệ thống/Môn-Chế độ mặc định/Lưu thay đổi/Khu vực nguy hiểm), và hộp thoại Trợ giúp.
- Sửa 1 lỗi thứ tự tải trang phát hiện được trong lúc làm: tin nhắn chào mừng trước đây hiện ra TRƯỚC KHI tải xong tuỳ chọn ngôn ngữ đã lưu, nên luôn hiện tiếng Việt dù đã chọn tiếng Anh từ trước — đã sửa thứ tự tải (đợi tải xong tuỳ chọn trước, rồi mới hiện lời chào).
- Đã xác minh bằng cách kiểm tra HTML thật được server trả về (16 điểm kiểm tra) — không chỉ đọc code rồi tin là đúng.

**Còn thiếu (thành thật liệt kê)**: nội dung bên trong overlay Thẻ ghi nhớ/Sổ lỗi sai/Quiz/Kế hoạch ôn tập/Trò chơi (tạo bởi JavaScript, hàng trăm chuỗi tiếng Việt rải trong nhiều hàm khác nhau), các hộp thoại `confirm()`/`alert()`, và toàn bộ trang quản trị Developer/Lab vẫn còn tiếng Việt cố định — dịch hết chỗ này là khối lượng công việc lớn, cần 1 đợt riêng nếu bạn cần.

## 30. Rắn Săn Chữ (Snake Quiz) — game mới, đã kiểm thử độc lập bằng Node.js trước khi ghép vào

Ở bản trước mình có nói Snake cần "canvas + vật lý" nên tạm gác lại. Xem lại kỹ hơn thì Snake thực chất là 1 máy trạng thái RỜI RẠC (di chuyển theo lưới ô vuông, không có vật lý/va chạm pixel liên tục như Flappy Bird) — hoàn toàn kiểm thử được bằng logic thuần, giống hệt cách đã làm với Đố Vui Tính Nhanh. Vì vậy lần này làm thật:

- Điều khiển bằng phím mũi tên/WASD, nút bấm trên màn hình, hoặc vuốt (điện thoại). Ăn 🔵 để lớn lên (+10 điểm), ăn 🟡 — mồi đặc biệt hiện đúng đáp số 1 phép tính đang hỏi phía trên — được +30 điểm và tính 1 câu đúng. Va tường hoặc tự đâm vào thân là thua.
- 3 mức độ khó (Dễ/Trung bình/Khó) — kích thước bàn cờ và tốc độ khác nhau. Câu hỏi phép tính dùng LẠI đúng bộ sinh câu hỏi đã kiểm thử 60.000 lần ở Đố Vui Tính Nhanh (`generateQmQuestion`), không viết lại logic mới có nguy cơ có lỗi.
- Bỏ lỡ 1 câu (hết giờ chưa ăn kịp mồi đáp số) tính là "sai" phép tính đó — cuối ván tự lưu vào Sổ lỗi sai, y hệt cơ chế Đố Vui Tính Nhanh.
- Thành tựu mới: 🐍 **Trăn Thần** (đạt độ dài 15 ô trong 1 ván).

**Mức độ kiểm thử đã làm** (quan trọng, vì đây là code có logic va chạm — dễ có lỗi nếu không cẩn thận):
1. Viết riêng 4 hàm logic thuần (tạo game, di chuyển, ăn mồi, va chạm) — test độc lập bằng Node.js với 9 nhóm kiểm tra: di chuyển cơ bản, ăn mồi thường/mồi câu hỏi, va chạm ở cả 4 hướng tường, tự đâm thân, luật "không quay đầu 180°", **5.000 lần thử** xác nhận mồi không bao giờ xuất hiện đè lên thân rắn, và **200 ván mô phỏng chơi ngẫu nhiên** (di chuyển ngẫu nhiên liên tục hàng nghìn lượt) không phát hiện lỗi nào.
2. **Bắt được 1 lỗi thật** trong lúc làm: khi ghép route `/api/games/snake/submit` vào code, thao tác chỉnh sửa lỡ xoá mất dòng `@app.route(...)` của route `/api/games/stats` (route liệt kê điểm cao nhất) — phát hiện ngay vì bài test tự động báo lỗi 404, sửa lại xong mới báo cáo là "xong".
3. Sau khi ghép toàn bộ vào `app.py`, **trích lại ĐÚNG đoạn code JS thật đã lưu trong file** (không phải bản nháp) và chạy lại bộ 9 test đó lần nữa để chắc chắn không có sai khác trong lúc gõ — vẫn qua hết.
4. Kiểm tra cú pháp toàn bộ ~122KB JavaScript của trang chính bằng trình phân tích cú pháp Node.js thật (không chỉ `python3 -m py_compile`, vì lệnh đó CHỈ kiểm tra phần Python bao quanh, không đụng tới nội dung JS bên trong).

## 31. Giữ app "luôn chạy" trên Render (miễn phí) — và 1 CẢNH BÁO quan trọng cho database

Đã kiểm tra thông tin mới nhất từ Render (tháng 8/2026) trước khi trả lời, không dựa vào trí nhớ cũ:

**Vì sao app bị "ngủ"**: gói Free của Render tự động dừng (spin down) web service sau **15 phút không có request nào tới** — lần truy cập tiếp theo sẽ mất khoảng **30-60 giây** để "thức dậy" (cold start). Đây là hành vi CỐ Ý của Render để tiết kiệm tài nguyên gói miễn phí, không phải lỗi của code.

**⚠️ QUAN TRỌNG — ảnh hưởng trực tiếp tới `studymate.db`**: gói Free của Render có **ổ đĩa tạm (ephemeral filesystem)** — nghĩa là MỌI thay đổi trên hệ thống file (bao gồm file `studymate.db`) **sẽ bị XOÁ SẠCH mỗi khi service khởi động lại, deploy lại, hoặc "ngủ rồi thức dậy"**. Điều này ngược hẳn với điều bạn từng hỏi ở mục 27 ("có mất tài khoản khi lên live không") — câu trả lời "không mất" ở mục đó ĐÚNG khi bạn tự quản lý server (VPS, máy riêng...), nhưng **SAI nếu deploy trên gói Free của Render**, vì chính Render sẽ xoá file database, không phải do code của app.

### Cách khắc phục (xếp theo mức độ đáng tin cậy)

1. **Đáng tin cậy nhất — nâng cấp gói trả phí + gắn Persistent Disk**: gói Starter ($7/tháng) giữ service chạy liên tục KHÔNG bị ngủ, nhưng **vẫn cần gắn thêm "Persistent Disk"** (tính phí riêng theo dung lượng, xem giá mới nhất trong Dashboard Render lúc bạn tạo) thì `studymate.db` mới thật sự được giữ lại qua các lần deploy/restart. Thiếu bước gắn Disk thì dù trả phí, database vẫn bị mất khi deploy lại.
2. **Miễn phí nhưng KHÔNG giải quyết được việc mất database** — dùng dịch vụ ping ngoài để "đánh thức" liên tục: đã thêm route mới **`GET /health`** (trả về `{"status":"ok"}` ngay lập tức, không cần đăng nhập, không đụng tới database) — đăng ký 1 tài khoản miễn phí ở **UptimeRobot** hoặc **cron-job.org**, cấu hình ping vào `https://<tên-app-của-bạn>.onrender.com/health` mỗi 10-14 phút. Cách này giữ app không bị spin-down do hết 15 phút rảnh, nhưng **KHÔNG ngăn được** việc mất dữ liệu nếu bạn chủ động deploy lại code — mỗi lần deploy lại, `studymate.db` vẫn về lại trạng thái rỗng trên gói Free.
   - Lưu ý thêm: gói Free chỉ có 750 giờ máy chủ miễn phí/tháng — ping liên tục 24/7 gần như dùng hết đúng số giờ đó (1 tháng ~730-744 giờ), nên gần như không còn dư cho service khác cùng workspace.
3. **Giải pháp lâu dài, đúng bản chất nhất**: chuyển từ SQLite (file) sang **Render PostgreSQL** (có gói miễn phí, nhưng hết hạn sau 30 ngày rồi cần nâng cấp; hoặc gói Postgres trả phí nhỏ) — Postgres là database MẠNG thật, không nằm trên ổ đĩa tạm của web service nên không bị mất khi deploy lại. Đây là thay đổi kiến trúc (SQLite → Postgres), **KHÔNG nằm trong phạm vi đã làm lần này** — cho mình biết nếu bạn muốn triển khai, đây sẽ là 1 việc riêng khá lớn (đổi toàn bộ các câu lệnh `sqlite3`/`? placeholder` sang thư viện Postgres).

**Tóm lại**: nếu bạn định deploy thật lên Render, mình khuyên **ưu tiên tìm hiểu mục 3 (chuyển sang Postgres)** hoặc **mục 1 (Starter + Persistent Disk)** — đừng chỉ dùng cách ping (mục 2) rồi yên tâm là dữ liệu an toàn, vì nó không phải vậy.

## 32. Đã làm: cho phép chỉ định nơi lưu database qua biến môi trường `DB_PATH` (phục vụ mục 1 ở trên)

Không chuyển sang Postgres ngay được (đã thử cài thư viện kết nối Postgres trong môi trường code của mình — không có mạng để cài, nghĩa là mình không thể CHẠY THỬ một bản chuyển đổi Postgres trước khi giao cho bạn, mà app này đã có hàng trăm câu SQL viết theo cú pháp SQLite rải khắp code — sửa mù mà không test được là kiểu việc mình tránh làm suốt từ đầu). Vì vậy làm phần **nhỏ, an toàn, test được đầy đủ**: cho phép trỏ file `studymate.db` ra NGOÀI thư mục code, để gắn vào Persistent Disk của Render (mục 1).

**Đã thay đổi**: nếu có biến môi trường `DB_PATH`, app dùng đúng đường dẫn đó để lưu database; nếu không đặt gì (mặc định), hành vi giữ NGUYÊN như trước (file `studymate.db` cạnh `app.py`) — không ảnh hưởng gì tới cách bạn đang chạy local.

**Đã kiểm thử**: (1) hành vi mặc định không đổi khi chưa đặt `DB_PATH`; (2) đặt `DB_PATH` trỏ ra 1 thư mục khác (giả lập ổ đĩa gắn rời) — database được tạo đúng chỗ, không phải cạnh `app.py`; (3) **giả lập nguyên 1 lần "deploy lại"**: xoá sạch thư mục code (đúng như Render làm với ổ đĩa tạm), giữ nguyên thư mục "đĩa gắn rời" — tài khoản tạo trước đó **vẫn còn nguyên** sau khi import lại app từ thư mục code mới.

### Các bước làm trên Render (bạn tự thao tác trong Dashboard, mình không bấm hộ được)
1. Service của bạn phải đang ở gói **trả phí** (Starter trở lên) — gói Free không cho gắn Persistent Disk.
2. Vào service trên Render → tab **"Disks"** → **Add Disk**. Đặt tên bất kỳ (vd `data`), **Mount Path** đặt là `/data`, dung lượng 1GB là quá đủ cho SQLite (database hiện tại của bạn chỉ vài trăm KB).
3. Vào tab **"Environment"** → thêm biến môi trường mới: **Key** = `DB_PATH`, **Value** = `/data/studymate.db`.
4. Deploy lại 1 lần — từ giờ, mọi lần deploy/restart sau đó, database sẽ đọc/ghi đúng vào ổ đĩa bền vững này, không bị xoá nữa.
5. **Lưu ý nếu bạn ĐÃ có dữ liệu thật trên gói Free trước đó**: dữ liệu cũ nằm trong ổ đĩa tạm, sẽ mất khi bạn đổi cấu hình này (không có cách "chuyển" dữ liệu cũ ra ngoài trừ khi tải file `studymate.db` xuống thủ công trước khi đổi — Render có mục "Shell" trong Dashboard để bạn `cat`/tải file ra nếu cần giữ lại dữ liệu test hiện có).

⚠️ Nhắc lại 1 lần nữa: cách này **cần trả phí** (Starter ~$7/tháng + phí Disk riêng, xem giá thật lúc bạn tạo trong Dashboard). Nếu muốn 100% miễn phí và chấp nhận dữ liệu có thể mất, giữ nguyên cấu hình hiện tại (không đặt `DB_PATH`) + UptimeRobot (mục 31) là đủ để test/demo.

## 33. Super Admin chỉ dành riêng cho 1 tài khoản + Dev Lab điều khiển được cả 3 trò chơi (mới)

### 🔒 Super Admin — chỉ "BlackadaNutella" (hoặc tài khoản bạn cấu hình)
Trước đây: 1 Super Admin có thể cấp Super Admin cho BẤT KỲ tài khoản nào khác. Giờ:
- Hằng số `SUPER_ADMIN_USERNAME` (mặc định `"BlackadaNutella"`, đổi được qua biến môi trường cùng tên trong `.env`) là tài khoản DUY NHẤT được phép giữ vai trò này.
- Mỗi lần khởi động server: tự động nâng tài khoản đó lên Super Admin (nếu đã tồn tại và chưa phải Super Admin) — **VÀ** tự động hạ bất kỳ tài khoản NÀO KHÁC đang lỡ có vai trò Super Admin xuống Admin.
- Giao diện quản lý vai trò: không ai — kể cả 1 Super Admin khác — cấp được Super Admin cho tài khoản nào ngoài tài khoản đã chỉ định, dù có cố tình bấm qua form.
- Đã kiểm thử đầy đủ: tự nâng đúng tài khoản, tự hạ tài khoản "lạ" từng có Super Admin, chặn cấp Super Admin cho tài khoản khác qua giao diện (kèm thông báo rõ ràng), các thao tác đổi vai trò bình thường khác (User↔Developer, Admin) không bị ảnh hưởng.
- ⚠️ File `studymate.db` bạn gửi kèm chỉ có 1 tài khoản (`developer`) — không khớp với dữ liệu thật đang chạy trên Render (thấy trong ảnh chụp màn hình có `BlackadaNutella`, `HongMaiYnNhi`). File đó là bản cũ/local, không phải database thật — không cần (và không nên) ghi đè database thật bằng file này. Cứ deploy `app.py` mới lên chỗ database thật đang chạy, việc nâng quyền sẽ tự xảy ra ở lần khởi động kế tiếp.

### 🎮 Dev Lab giờ điều khiển được cả 3 trò chơi
Áp dụng đúng khuôn mẫu đã làm với Rắn Săn Chữ cho **Đố Vui Tính Nhanh** và **Lật thẻ ghi nhớ**:
- Cả 3 đều có flag riêng trong `/developer/lab` (`game_snake_quiz`, `game_quick_math`, `game_memory_match`), mặc định `public` (không đổi gì cho người dùng hiện tại).
- Ẩn 1 trò: biến mất khỏi thư viện trò chơi VÀ khỏi MỌI lối vào khác (vd nút "🎮 Lật thẻ" trong trang chi tiết bộ thẻ), route API tương ứng cũng từ chối yêu cầu trực tiếp (403) — không chỉ ẩn trên giao diện mà bấm thẳng API vẫn né được.
- Trạng thái `internal`: chỉ Developer trở lên vẫn thấy/chơi được (tự test). Trạng thái `off`: tắt hẳn cho TẤT CẢ, kể cả Developer — đúng nghĩa "công tắc khẩn cấp".
- Đã kiểm thử: bật/tắt từng trò riêng lẻ, tắt cả 3 cùng lúc, xác nhận trang Games tab vẫn hiển thị bình thường (không lỗi JS) kể cả khi rỗng hoàn toàn.

**Chưa làm** (như đã nói ở lượt trước, phạm vi lớn hơn hẳn 3 trò chơi): Flashcards, Sổ lỗi sai, Quiz Generator, Kế hoạch ôn tập chưa được nối vào Dev Lab — mỗi tính năng đó có nhiều route API hơn, cần 1 đợt riêng để làm đúng mức cẩn thận đã áp dụng cho 3 trò chơi. Nói mình biết cái nào muốn làm tiếp theo.

## 34. Quên mật khẩu + Chống dò mật khẩu/spam đăng ký (mới) — 2 lỗ hổng nền tảng, không phải tính năng mới

Bạn hỏi "sản phẩm còn thiếu gì" — thay vì thêm tính năng mới, mình chọn vá 2 lỗ hổng thực sự nguy hiểm cho 1 sản phẩm đang có người dùng thật.

### 🔑 Quên mật khẩu (Mã khôi phục)
Trước đây: quên mật khẩu = hết cách, phải nhờ Admin sửa tay trong database. Không có hạ tầng gửi email (SMTP) nên mình không dùng cách "gửi email đặt lại mật khẩu" — không thể kiểm thử việc gửi email thật trong môi trường của mình, mà mình không giao thứ gì chưa test được. Thay vào đó dùng **mã khôi phục tự chứa**:
- Đăng ký xong, hiện ra **đúng 1 lần** 1 mã dạng `XXXX-XXXX-XXXX` (bỏ ký tự dễ nhầm 0/O/1/I) — StudyMate chỉ lưu bản băm (hash), giống hệt mật khẩu, không lưu lại bản gốc nên không tự hiện lại lần 2 được.
- Trang `/forgot-password`: nhập username + mã + mật khẩu mới → đặt lại được ngay, không cần đăng nhập trước.
- Dùng mã xong tự đổi sang **mã mới** (mã cũ hết hạn ngay) — đúng thông lệ cho mã dùng-một-lần.
- Tài khoản **có sẵn từ trước** (tạo trước khi có tính năng này) chưa có mã — vào Cài đặt → "Tạo mã khôi phục mật khẩu mới" để tự tạo lần đầu, không cần Admin can thiệp.
- Không áp dụng cho tài khoản khách/đăng nhập Google (không dùng mật khẩu nên không có gì để "khôi phục").
- Đã test đầy đủ: đăng ký → thấy mã → dùng mã đặt lại mật khẩu → mã cũ bị từ chối → đăng nhập bằng mật khẩu mới → dùng mã mới đặt lại lần nữa vẫn được.

### 🛡️ Chống dò mật khẩu / spam đăng ký
- `/login`: giới hạn theo **cặp (IP, tên đăng nhập)** — 8 lần sai/15 phút — KHÔNG tính theo IP đơn thuần, để 1 bạn gõ sai mật khẩu nhiều lần không khoá luôn cả lớp học chung 1 mạng trường (đã kiểm thử đúng kịch bản này: 5 học sinh khác nhau đăng nhập đúng liên tiếp từ CÙNG 1 địa chỉ IP — không ai bị chặn). Có thêm giới hạn tổng theo IP (60 lần/15 phút) chỉ để chặn bot dò quét nhiều tài khoản khác nhau.
- Đăng nhập đúng thì tự xoá bộ đếm sai (không cộng dồn oan).
- `/register`: giới hạn 20 lượt/giờ theo IP — đủ rộng cho cả lớp cùng đăng ký trong 1 tiết học, vẫn chặn được spam tạo tài khoản hàng loạt.
- Đã kiểm thử cả 2 chiều: kịch bản tấn công (bị chặn đúng lúc, kể cả thử mật khẩu ĐÚNG cũng bị chặn nếu đang trong thời gian giới hạn — tránh dò được thời điểm) VÀ kịch bản dùng bình thường (không bị chặn oan).
- ⚠️ Giới hạn: bộ đếm nằm trong RAM của tiến trình — nếu sau này chạy nhiều worker process (`gunicorn -w 4`), mỗi worker đếm riêng, ngưỡng thực tế sẽ cao hơn số cấu hình. Đủ chặn spam/bot thông thường; quy mô lớn hơn cần Redis.

## 35. Bảng Tiến Độ Học Tập (Progress Dashboard) — "đột phá" không phải bằng tính năng mới, mà bằng KẾT NỐI những gì đã có (mới)

Bạn hỏi "còn thiếu gì, cần thứ đột phá" — nhận định của mình: sau bao nhiêu lượt, app đã có RẤT NHIỀU dữ liệu học tập (XP/streak, Sổ lỗi sai, điểm Quiz, tiến độ Kế hoạch ôn tập, điểm cao 3 trò chơi, môn học hay hỏi nhất) — nhưng **mỗi thứ nằm 1 nơi riêng biệt**, học sinh không có chỗ nào xem TỔNG QUAN chính mình đang học thế nào. Đây chính là hướng "hiểu người học, không chỉ hiểu câu hỏi" mà bạn từng nói tới rất lâu trước đây trong cuộc trò chuyện này — giờ mới thực sự làm được, vì cần đủ dữ liệu từ các tính năng đã xây trước đó.

**Cách vào**: bấm vào khung XP/streak ở sidebar (giờ có thể bấm được, trước đây chỉ để xem) → mở "Tiến độ học tập của em".

**Nội dung tổng hợp từ TẤT CẢ hệ thống đã có, không cần xây dữ liệu mới**:
- Cấp độ / Streak (dài nhất) / Tổng XP.
- **Môn học hay hỏi nhất** — đếm từ lịch sử chat, hiện dạng thanh ngang.
- **Điểm yếu cần ôn** — lấy từ Sổ lỗi sai (chỉ lỗi CHƯA khắc phục), có nút "Ôn lại" ngay tại chỗ.
- **Kết quả Quiz gần đây** — điểm trung bình + 5 lần gần nhất.
- **Kế hoạch ôn tập** — % hoàn thành từng kế hoạch, bấm vào mở thẳng chi tiết.
- **Điểm cao trò chơi** — cả 3 game.
- **Bộ sưu tập thành tựu đầy đủ** — cả đã mở khoá lẫn chưa (mờ đi), không chỉ liệt kê cái đã có.

**"Gợi ý hôm nay"** — tính bằng LUẬT ĐƠN GIẢN (không gọi thêm AI, nhanh, miễn phí, kết quả đoán trước được để test), ưu tiên theo mức độ khẩn cấp thực tế:
1. Có lỗi sai lặp lại ≥2 lần chưa khắc phục → gợi ý ôn đúng lỗi đó, kèm nút hành động.
2. Streak về 0 nhưng từng có streak dài → khuyến khích quay lại.
3. Có kế hoạch ôn tập bị trễ tiến độ → gợi ý sắp xếp lại.
4. Còn 1 ngày nữa là đạt mốc streak tiếp theo → nhắc đừng bỏ lỡ.
5. Không có gì đặc biệt → lời động viên chung theo cấp độ hiện tại.

Đã kiểm thử cả 5 nhánh gợi ý bằng dữ liệu giả lập tương ứng, xác nhận đúng thứ tự ưu tiên, và kiểm thử toàn bộ luồng tổng hợp dữ liệu thật (chat nhiều môn, lỗi sai lặp lại, làm quiz, tạo kế hoạch, chơi game) cho ra đúng số liệu ở mọi mục.

⚠️ **Tự bắt được 1 lỗi nghiêm trọng trong lúc làm**: 1 thao tác chỉnh sửa đã vô tình xoá mất dòng khai báo `function openPalette() {` (bảng lệnh nhanh Ctrl+K), làm phần thân hàm bị "mồ côi" — biến thành code chạy ngay lúc tải trang thay vì đợi người dùng bấm. Việc chạy lại kiểm tra cú pháp JS bằng Node.js sau MỖI thay đổi lớn (không chỉ tin `python3 -m py_compile`, vì lệnh đó chỉ kiểm tra phần Python bao quanh) đã bắt được lỗi này ngay lập tức — đã sửa và xác nhận lại bảng lệnh nhanh hoạt động bình thường trước khi giao.

## 36. Sửa lỗi ĐIỆN THOẠI: khung chat với AI bị ẩn mất (mới, quan trọng)

Bạn báo: dùng trên điện thoại thì phần chat với AI bị ẩn, "quá cỡ". Đã tìm ra **2 lỗi CSS kinh điển của mobile cộng lại** — đều nằm ở trang chat, không phải lỗi dữ liệu:

**Lỗi 1 — `100vh` trên trình duyệt điện thoại KHÔNG bằng vùng nhìn thấy thật.**
Trang chat dùng `h-screen` (= `height: 100vh`) kèm `overflow: hidden` ở `body`. Trên Safari/Chrome điện thoại, `100vh` tính cả phần bị **thanh địa chỉ phía trên và thanh công cụ phía dưới che mất** — nghĩa là khung app cao hơn màn hình thật khoảng 100–150px. Vì `body` đang `overflow:hidden` nên phần thừa đó **không cuộn tới được** → ô nhập câu hỏi và phần dưới khung chat bị đẩy hẳn ra ngoài, đúng như bạn thấy.
→ Đã đổi sang **`100dvh`** (dynamic viewport height — tính đúng vùng đang nhìn thấy, tự co giãn khi thanh địa chỉ ẩn/hiện), vẫn giữ `100vh` làm dự phòng cho trình duyệt cũ.

**Lỗi 2 — thiếu `min-h-0` trong flex column.**
Trong CSS Flexbox, phần tử `flex-1` mặc định có `min-height: auto`, tức là **không co lại được nhỏ hơn nội dung bên trong**. Khi đoạn chat dài ra, khung `#chatPanel` cứ phình theo nội dung thay vì cuộn, đẩy ô nhập câu hỏi xuống dưới đáy màn hình.
→ Đã thêm `min-h-0` cho cả cột nội dung chính lẫn `#chatPanel`, và `flex-shrink-0` cho header + vùng ô nhập để 2 phần này không bao giờ bị bóp méo.

**Sửa thêm cho gọn trên màn hình nhỏ**: khoảng đệm `flex-1` giữa các nút ở header trước đây ép nút micro + đổi giao diện **xuống hẳn một dòng riêng** trên điện thoại (tốn thêm ~44px chiều cao vô ích). Giờ khoảng đệm đó chỉ bật từ màn hình lớn (`hidden lg:block`).

Đã kiểm chứng cả 9 điểm sửa có mặt đúng trong HTML trang chat, và chạy lại kiểm thử tổng thể (chat, lưu lịch sử, tiến độ, thẻ ghi nhớ, trò chơi, developer, đăng nhập) — không có gì hỏng.

> 📌 **Nhân tiện, về việc "mất lưu lịch sử trò chuyện" bạn báo trước đó**: bài kiểm thử ở trên có kiểm tra riêng phần này (gửi 1 tin nhắn → đọc lại `/api/conversations`) và **lịch sử được lưu đúng**. Nghĩa là code không có lỗi — càng củng cố nghi ngờ ở mục 31/32: dữ liệu bị mất là do **ổ đĩa tạm của Render** xoá file `studymate.db` mỗi lần deploy lại, chứ không phải app ghi sai. Cách xử lý triệt để vẫn là gắn Persistent Disk (mục 32) hoặc chuyển sang Postgres.

## 37. 👩‍🏫 LỚP HỌC (Teacher Mode) — bước nhảy từ "app cá nhân" thành "nền tảng cho cả lớp" (mới)

Bạn hỏi còn thiếu gì và muốn nâng lên tầm cao mới. Nhận định của mình: mọi tính năng tới giờ đều phục vụ **1 học sinh dùng một mình**. Thứ thay đổi được BẢN CHẤT sản phẩm — và cũng là mục đầu tiên trong Phase 2 do chính bạn đề ra — là **Teacher Mode**. Một giáo viên kéo theo cả lớp 40 học sinh; đó là khác biệt giữa "một công cụ" và "một nền tảng".

Điểm hay: nó **dùng lại gần như toàn bộ** những gì đã xây suốt các lượt trước (Quiz Generator, hệ chấm điểm, phân tích điểm yếu theo chủ đề, Sổ lỗi sai) — không phải xây lại từ đầu.

### Cách dùng (tab "Lớp học" mới)
- **Giáo viên**: Tạo lớp → nhận **mã mời 6 ký tự** (bỏ ký tự dễ nhầm 0/O/1/I/L để đọc to trong lớp/chép lên bảng không sai) → đọc mã cho học sinh.
- **Học sinh**: nhập mã → vào lớp ngay, không cần duyệt.
- **Giao bài**: giáo viên chọn 1 quiz **đã tạo ở tab Quiz** + đặt hạn nộp → cả lớp thấy bài tập. Không nhân bản đề — cả lớp làm chung 1 quiz, điểm tách nhau nhờ mã bài tập.
- **Học sinh làm bài** ngay trong app, chấm tự động, nộp xong quay lại lớp thấy điểm liền.

### Bảng điều khiển lớp (đúng như mockup bạn từng vẽ)
Giáo viên thấy: **sĩ số · điểm trung bình lớp · số học sinh cần chú ý**, kèm:
- **"Cả lớp yếu nhất phần này"** — gom `weak_topics` của mọi bài làm trong lớp, không cần gọi thêm AI (dữ liệu vốn đã có từ hệ chấm quiz).
- **Bảng từng học sinh**: đã làm bao nhiêu bài, điểm trung bình, tự gắn cờ ⚠️ **"cần chú ý"** (điểm TB dưới 50% hoặc chưa nộp bài nào).
- Mỗi bài tập: bao nhiêu em đã nộp / điểm trung bình.
- Mỗi học sinh chỉ tính **lần làm tốt nhất** cho mỗi bài tập (công bằng nếu cho làm lại).

### Về phân quyền & riêng tư — đã kiểm thử kỹ
- **Học sinh chỉ thấy điểm CỦA CHÍNH MÌNH.** Đã viết test xác nhận API trả về cho học sinh **không hề chứa** danh sách điểm bạn khác, điểm trung bình lớp, phân tích điểm yếu lớp, và **không chứa cả mã mời lớp**.
- Chỉ giáo viên của lớp mới giao được bài. Người ngoài không xem được lớp (403).
- **Chống giả mạo**: đã test kịch bản người ngoài cố gửi mã bài tập của lớp mình không tham gia để chèn điểm vào — bị chặn, sĩ số và số bài nộp của lớp không đổi.
- Không tạo vai trò "teacher" riêng: **bất kỳ tài khoản nào cũng tạo được lớp** (giáo viên thật, hoặc học sinh lập nhóm học chung) — đơn giản hơn và không đụng vào hệ phân quyền user/developer/admin/super_admin đang chạy ổn định.

### 🐞 Hai lỗi thật do kiểm thử phát hiện (nếu không test thì đã giao hàng lỗi)
1. **Học sinh không mở nổi bài tập được giao.** Route xem đề (`/api/quizzes/<id>`) và route nộp bài đều chỉ cho **chủ sở hữu quiz** (tức giáo viên) truy cập — nghĩa là học sinh bấm "Làm bài" sẽ nhận 404, tính năng vô dụng hoàn toàn. Đã mở thêm đúng một nhánh: cho phép nếu quiz đó được giao cho lớp mà người dùng là **thành viên** — và kiểm thử lại rằng **người ngoài lớp vẫn bị chặn**.
2. Một lỗi trong chính bài test của mình (dùng tên đăng nhập 2 ký tự, dưới mức tối thiểu 3) làm test báo sai — đáng nói vì trang đăng ký lỗi cũng trả HTTP 200, nên chỉ kiểm tra mã trạng thái là chưa đủ. Đã sửa test thành **gọi một API cần đăng nhập để xác nhận thật sự đã đăng nhập**, chắc chắn hơn.

## 38. Sửa TẬN GỐC lỗi công thức toán — lần này tìm ra nguyên nhân thật (mới, quan trọng)

Bạn gửi lại một câu trả lời của AI đầy `( a = 0 )`, `[ r = \dfrac{a}{b} ]`, `(b^(2025))`... và nói đúng: **"như này rồi sao người đọc được?"**

**Thú nhận trước**: ở mục 18 và 21 mình đã 2 lần nói "đã sửa lỗi công thức" — nhưng cả 2 lần đó mình chỉ **vá phần ngọn** (lưới an toàn đổi `\sqrt{}` thành `√()`, và rút gọn lời dặn AI). Mình **chưa từng tìm ra nguyên nhân thật**, nên lỗi vẫn còn nguyên. Lần này thì tìm ra rồi:

```javascript
bubble.innerHTML = marked.parse(text);   // ← Markdown chạy TRƯỚC
renderMathIn(bubble);                     // ← KaTeX chạy SAU, đã quá muộn
```

**Nguyên nhân gốc**: bộ dựng Markdown (`marked`) chạy **trước** KaTeX. Mà theo đúng chuẩn Markdown, dấu `\` đứng trước một dấu câu là **ký tự thoát** — nên `marked` **nuốt mất dấu `\`** trong `\(`, `\)`, `\[`, `\]`. Tới lượt KaTeX thì **dấu hiệu nhận biết công thức đã bị xoá sạch**, nó không còn gì để dựng. Đó chính xác là lý do học sinh thấy `( a = 0 )` thay vì công thức đẹp, và `\dfrac` mất dấu gạch chéo.

**Còn một lỗi ngầm nữa mà bài kiểm thử phát hiện thêm**: dấu `_` trong công thức (vd `y_1`) bị Markdown hiểu là **in nghiêng** → mọi **chỉ số dưới** trong công thức đều đang bị hỏng âm thầm bấy lâu nay mà không ai để ý.

**Cách sửa đúng**: tách các đoạn công thức ra thay bằng mã giữ chỗ **TRƯỚC** khi chạy Markdown, rồi trả lại nguyên văn **SAU** khi Markdown xong (`protectMath` / `restoreMath` / `renderMarkdownSafe`). KaTeX giờ nhận được công thức **y hệt** những gì AI viết ra.

**Kiểm thử đã làm**:
- Viết bài test tái hiện đúng hành vi nuốt dấu `\` của Markdown, chạy trên **chính đoạn văn bản trong ảnh bạn gửi** — xác nhận cách cũ hỏng, cách mới giữ nguyên 100%.
- Trích **hàm thật từ `app.py`** (không phải bản nháp) chạy lại lần nữa để chắc chắn không sai khác lúc gõ.
- Xác nhận Markdown thường (in nghiêng, đậm...) vẫn hoạt động bình thường, và tiền tệ kiểu `50$ và 100$` không bị nhầm thành công thức.

⚠️ **Một rủi ro tự bắt được trong lúc sửa**: bản đầu tiên mình dùng regex **lookbehind** `(?<!...)`. Safari trên iPhone **cũ hơn iOS 16.4** không hỗ trợ cú pháp này — và nó không chỉ làm sai regex, mà gây **lỗi cú pháp làm chết TOÀN BỘ JavaScript của trang** (app trắng xoá, không dùng được gì). Với đối tượng học sinh THCS dùng máy cũ thì đây là rủi ro thật. Đã viết lại regex không dùng lookbehind, kiểm thử 6 trường hợp (công thức 1 ký tự, công thức có dấu cách, tiền tệ...) đều đúng.

## 39. Nâng cấp HIỆU NĂNG — 2 điểm nghẽn thật, có số liệu đo (mới)

Bạn cho quyền tự chọn nâng cấp. Mình **không thêm tính năng mới** — app đã quá nhiều tính năng rồi. Thay vào đó mình đi tìm điểm nghẽn hiệu năng thật, và tìm ra 2 cái nghiêm trọng, cả hai đều **càng dùng lâu càng tệ**:

### 🗄️ 1. Database không có MỘT chỉ mục (index) nào — nhanh hơn **48 lần** sau khi sửa
Kiểm tra ra: **29 bảng, 0 index**, trong khi có **~38 truy vấn lọc theo `user_id`**. Nghĩa là mỗi lần mở lịch sử chat, bảng tiến độ, hay trang developer, SQLite phải **quét toàn bộ bảng từ đầu tới cuối**.

Điểm nguy hiểm: lúc ít dữ liệu thì không ai thấy gì — nhưng càng dùng lâu càng chậm dần đều, và **chậm nhất đúng với tài khoản chăm học nhất** (nhiều dữ liệu nhất). Tức là hệ thống đang phạt oan chính những học sinh dùng app nhiều nhất.

Đã thêm **27 index** cho mọi đường truy vấn nóng. **Số liệu đo thật** (giả lập quy mô trường học: 800 tài khoản, ~48.000 đoạn chat, 120.000 lượt hỏi, mỗi phép đo 200 lượt truy cập):

| Thao tác | Không index | Có index | Nhanh hơn |
|---|---:|---:|---:|
| Mở lịch sử chat | 393 ms | 9 ms | **43×** |
| Bảng tiến độ (thống kê môn) | 1.177 ms | 22 ms | **54×** |
| Sổ lỗi sai | 82 ms | 3 ms | **27×** |
| **Tổng** | **1.652 ms** | **34 ms** | **48×** |

Không chỉ tạo index rồi tin là xong — đã dùng `EXPLAIN QUERY PLAN` xác nhận SQLite **thật sự dùng** chúng (`SEARCH ... USING INDEX idx_conv_user`) chứ không phải bỏ qua.

### ⚡ 2. Mỗi token nhận về đều dựng lại TOÀN BỘ câu trả lời
Trong lúc AI trả lời, cứ **mỗi token** nhận được là chạy lại Markdown + KaTeX **trên cả bài từ đầu tới cuối**. Câu trả lời 1.500 token = 1.500 lần dựng lại một chuỗi ngày càng dài — khối lượng tính toán tăng theo **bình phương** độ dài.

Hệ quả: càng về cuối câu trả lời càng giật, đúng vào lúc học sinh đang chờ đọc — tức là **trải nghiệm cốt lõi của app**. Trên điện thoại yếu thì tệ hơn nhiều.

Đã sửa: gom các token đến liên tiếp lại, chỉ vẽ **tối đa 1 lần mỗi khung hình** (`requestAnimationFrame`). Mắt người vẫn thấy chữ chạy mượt y như cũ, nhưng khối lượng tính toán giảm rất nhiều. Lượt vẽ **cuối cùng luôn chạy ngay lập tức**, không hoãn, để nội dung cuối cùng chắc chắn đầy đủ và chính xác.

> 📌 Cả 2 nâng cấp này đều **không đổi gì về giao diện hay cách dùng** — học sinh không thấy nút mới nào, chỉ thấy app nhanh hơn. Đó đúng là loại nâng cấp mà một sản phẩm đã nhiều tính năng như thế này đang cần nhất.

## 40. Sửa lỗi Ghim / Xoá đoạn chat không bấm được trên điện thoại (mới)

Bạn báo phần **ghim** và **xoá riêng lẻ** đoạn chat không hoạt động. Kiểm tra kỹ:

**Backend hoàn toàn bình thường** — đã viết test gọi thẳng API: ghim → kiểm tra trong database (`pinned=1`), bỏ ghim → về `0`, xoá riêng lẻ → đúng 1 đoạn biến mất, các đoạn khác còn nguyên. Tất cả đều đạt. Vậy lỗi nằm ở giao diện.

**Nguyên nhân thật — đè lớp (z-index)**:

| Thành phần | z-index |
|---|---|
| Sidebar (trên điện thoại) | **50** |
| Menu 3 chấm `.conv-menu` | **45** ← thấp hơn |

Menu mở ra ngay bên trong vùng ngang của sidebar, mà z-index lại THẤP HƠN sidebar → **sidebar (nền đục) vẽ đè lên menu**. Kết quả: menu vừa không nhìn thấy, vừa không bấm được.

Vì sao trước đây vẫn dùng được? Trên **máy tính** sidebar là `position: static` — mà z-index **không có tác dụng** với phần tử static — nên menu vẫn nổi lên trên bình thường. Chỉ trên **điện thoại** sidebar mới là `position: fixed`, lúc đó z-index mới có hiệu lực và gây ra lỗi. Đúng kiểu lỗi chỉ lộ ra khi dùng điện thoại.

**Đã sửa**:
- `z-index: 45` → **55** (trên sidebar 50, dưới hộp thoại 60).
- `position: absolute` → **fixed**, cho khớp với toạ độ lấy từ `getBoundingClientRect()` (vốn tính theo màn hình), đồng thời tránh bị cắt bởi `overflow:hidden` của phần tử cha.
- **Sửa thêm một lỗi ngầm chưa ai báo**: code cũ giả định menu luôn cao đúng 260px. Menu có nhiều dự án thì cao hơn → phần cuối (đúng chỗ nút **"Xoá đoạn chat"**) bị tràn ra ngoài màn hình, tức là nút xoá có thể bấm không tới ngay cả khi menu đã hiện. Giờ đo kích thước **thật** sau khi menu được gắn vào trang, và nếu không đủ chỗ bên dưới thì **tự mở ngược lên trên**.
- Đã mô phỏng 5 kích thước màn hình thật (iPhone SE, Android 360px, máy tính) kèm 2 trường hợp khó nhất — chat sát đáy màn hình và menu rất dài — xác nhận menu **luôn nằm trọn trong màn hình**.

## Chưa làm (nằm ngoài phạm vi yêu cầu lần này)
Phần đầu prompt gốc của bạn từng có yêu cầu dựng lại toàn bộ thành một sản phẩm Next.js/TypeScript quy mô lớn (nhiều trang, Dashboard, Blog, Pricing...). Bản cập nhật này vẫn giữ nguyên nền tảng Flask hiện có của bạn. Nếu bạn vẫn muốn bản Next.js quy mô lớn, đó sẽ là một dự án tách riêng — cho mình biết nếu bạn muốn triển khai.

### Về bản đặc tả 41 mục (StudyMate AI — full ecosystem)
Đã đọc kỹ toàn bộ. Ngoài Phase 1 (mục 23 ở trên), các phần sau **chưa làm** — liệt kê rõ để bạn quyết định cái nào làm tiếp, mỗi cái đều đủ lớn để là 1 đợt phát triển riêng:

- **Phase 2**: Teacher Mode (lớp học/giao bài/chấm/thống kê học sinh), Smart Notes (ghi chú có Markdown/LaTeX/tag/thư mục), Focus Mode (Pomodoro/nhạc nền/thống kê phiên học), Voice Mode (hội thoại bằng giọng nói 2 chiều).
- **Phase 3**: AI Tutor Store (marketplace công khai cho Custom Tutor — hiện Custom Tutor chỉ dùng nội bộ, dev tự tạo cho mình), Developer Platform đầy đủ (Webhooks, Knowledge Base, Deployments), Share System (link công khai xem lại bài giải/quiz), Analytics chi tiết (DAU/WAU/MAU, retention).
- **Phase 4**: Quick Launcher (Alt+Space mở cửa sổ nhanh — giới hạn kỹ thuật: trình duyệt KHÔNG cho web app đăng ký phím tắt toàn hệ điều hành, chỉ có thể bắt phím tắt khi tab đang mở), Screen Capture + OCR, Select-to-Explain (bôi đen văn bản ngoài trang để hỏi AI — cũng cần extension trình duyệt, không làm được thuần bằng web app), Command Palette (Ctrl+K).
- Rải rác trong đặc tả còn có: Matching/tự luận trong Quiz (cần AI chấm chủ quan, tốn thêm lượt gọi AI mỗi lần chấm — khác hẳn 3 dạng đã làm), Language Learning mode riêng, Onboarding hỏi đáp lúc đăng ký lần đầu, notification center, global search xuyên suốt mọi loại dữ liệu, leaderboard (opt-in).

Không cái nào trong số này bị bỏ quên — chỉ là cần bạn xác nhận thứ tự ưu tiên trước khi mình bắt tay vào, để tránh lặp lại tình huống làm dở nhiều thứ cùng lúc.

Vài ý tưởng hợp lý để làm tiếp sau này (chưa làm, vì nằm ngoài yêu cầu lần này):
- Trang đổi mật khẩu cho tài khoản đăng nhập bằng mật khẩu (hiện chỉ có thể đổi qua thao tác thủ công trong DB).
- Rate limiting cho `/login`, `/register` để chống spam/brute-force khi deploy công khai (gợi ý: `flask-limiter`).
- Lưu "Chế độ suy nghĩ" đang chọn vào tuỳ chọn cá nhân để nhớ qua các lần đăng nhập (xem ghi chú cuối mục 11).
- Gia hạn tự động trừ tiền định kỳ, hoá đơn/biên lai điện tử, hoàn tiền (xem ghi chú cuối mục 14).
- Quiz trắc nghiệm tự sinh + chấm điểm từ bộ thẻ ghi nhớ, và các ý tưởng game khác (xem ghi chú cuối mục 17).
- Study Plan (kế hoạch ôn tập đa ngày tự động), Quiz Generator (chấm điểm + phân tích điểm yếu), AI Tutor Store (marketplace công khai) — xem mục "Về roadmap dài hạn" ở trên.
- Quiz trắc nghiệm tự sinh + chấm điểm từ bộ thẻ ghi nhớ, và các ý tưởng game khác (đố vui theo thời gian, thi đấu giữa 2 học sinh...) — xem ghi chú cuối mục 17.s
