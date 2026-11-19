# Hướng Dẫn Sử Dụng Hệ Thống Antidetect & Tự Động Hóa TikTok Beta / YouTube Shorts

## 1. Tổng Quan Hệ Thống
Hệ thống là giải pháp toàn diện kết hợp giữa:
- **Trình duyệt Antidetect Native C++ (Camoufox)**: Chống phát hiện bot sâu hơn nhiều so với Chrome thường / Patchright. Tự động spoof WebGL, Canvas, Audio, WebRTC, Fonts, Timezone, Geolocation theo Proxy.
- **Giao diện GPM-Login MMO Chuẩn**: Bảng quản lý Profile đa luồng, hỗ trợ nhóm, tag, proxy, đồng bộ trạng thái đăng nhập, hỗ trợ cả giao diện Sáng (Light) & Tối (Dark).
- **Auto Reup & Lách Bản Quyền Siêu Cấp**:
  - Tự động quét và tải video từ kênh YouTube Shorts nguồn.
  - Bộ chuyển đổi tỷ lệ & Nền mờ nghệ thuật (`blur_9_16`), cắt giữa tâm (`crop_9_16`), khung vuông (`crop_1_1`).
  - Giả lập Metadata Camera điện thoại thật (iPhone 15 Pro Max / Galaxy S23 Ultra) để vượt qua thuật toán quét 0-view và Shadowban của TikTok.
  - Quản lý video dạng Series / Tập tự động (`#Part1`, `#Part2`...).
- **Khám Sức Khỏe Kênh & Shadowban Auditor**: Quét TikTok Studio, tính điểm sức khỏe (/100) và xuất lộ trình tăng trưởng 10K Follow.
- **Bộ Lập Lịch Khung Giờ Vàng & Jitter**: Hẹn giờ đăng bài tự động buổi sáng (06:30 - 08:30) và buổi tối (18:30 - 21:30), lệch ngẫu nhiên $\pm 10$ phút tránh bị TikTok phát hiện bot.
- **Chế Độ Treo Máy Đêm (Keep-Awake)**: Giữ máy tính không bị ngủ (Sleep) khi cắm máy cày view và đăng bài qua đêm.

---

## 2. Hướng Dẫn Khởi Chạy Ứng Dụng

### Trên macOS
1. Kích hoạt môi trường ảo:
```bash
source .venv/bin/activate
```
2. Khởi chạy ứng dụng:
```bash
python app.py
```
*(Hoặc click đúp file `Chay_App_Mac.command` nếu đã tạo)*

### Trên Windows
1. Vào thư mục `Camoufox_Antidetect_Windows/`
2. Chạy `Cai_Dat_Windows.bat` (chỉ cần chạy lần đầu để tải Camoufox & thư viện).
3. Chạy `Chay_App_Windows.bat` để mở giao diện quản lý.

---

## 3. Các Tính Năng Trọng Tâm

### 3.1. Quản lý Profile Camoufox Antidetect
- **Tạo Profile mới**: Nhập tên, chọn hệ điều hành cần giả lập (Windows / macOS / Linux), độ phân giải màn hình, proxy (HTTP/Socks5).
- **Khởi động**: Mở trình duyệt độc lập với đầy đủ cookie, cache, fingerprint riêng biệt.
- **Chuyển đổi Proxy Bridge tự động**: Hỗ trợ SOCKS5/HTTP Proxy kèm xác thực username/password mà không bị lỗi xác thực trên Firefox.

### 3.2. Quét & Đăng Kênh Tự Động (TikTok Beta)
Tại tab **"Quét & Đăng Kênh"**:
1. Nhập **Kênh YouTube Shorts nguồn** (`@handle` hoặc link kênh).
2. Nhập **TikTok User đích** và chọn **Profile Camoufox** tương ứng.
3. Cấu hình các tùy chọn nâng cao:
   - **Thứ tự lấy video**: *Từ video cũ nhất* (đăng tuần tự theo thời gian) hoặc *Từ video mới nhất*.
   - **Tiêu đề / Caption**: *AI viết lại theo vùng SEO* (FR, US, MX, VN...) hoặc *Giữ tiêu đề gốc*.
   - **Cắt & Nền Video**:
     - `🎨 Nền mờ 9:16`: Đặt video gốc vào giữa, tạo nền mờ nghệ thuật tỷ lệ 1080x1920 (Khuyên dùng cho TikTok Shorts).
     - `✂️ Cắt trung tâm 9:16`: Phóng to và crop giữa tâm màn hình.
     - `📐 Khung vuông 1:1`: Chuyển đổi khung vuông.
     - `🎞 Giữ tỷ lệ gốc`.
   - **Series / Tập**: Nhập tên chuỗi (ví dụ: `ReviewPhim`) để hệ thống tự động gán `(Tập 1)`, `#Part1` và câu kêu gọi follow.
   - **🎲 Jitter Giờ Đăng (±10p)**: Ngẫu nhiên xê dịch thời điểm đăng bài quanh khung giờ vàng.
4. Bấm **"Lưu Ánh Xạ Kênh & Bắt Đầu Nuôi"**.

### 3.3. Đăng Video Tùy Chọn & Đăng Bù
- **🎯 Nút [Link]**: Đăng ngay 1 video YouTube Shorts cụ thể do bạn chọn cho kênh tương ứng.
- **🚀 Nút [Bù]**: Đăng bù 1 video ngay lập tức cho các kênh chưa đạt chỉ tiêu ngày.
- **🩺 Nút [Khám]**: Tự động mở TikTok Studio quét lượt xem các video gần nhất, phân tích và xuất điểm sức khỏe kênh.

### 3.4. Treo Máy Đêm (Keep-Awake)
- Bấm nút **"Treo Máy Đêm"** ở góc dưới cùng bên trái của Sidebar để bật tính năng giữ máy không bị ngủ (Sleep / Standby).
- Hỗ trợ cả macOS (`caffeinate`) và Windows (`SetThreadExecutionState`).

### 3.5. Báo Cáo Telegram & Kiểm Định Video
- Tự động gửi thông báo báo cáo hoàn thành đăng bài, duyệt kiểm định Content Check Lite (Xanh), và báo cáo KPI tổng hợp mỗi ngày về Telegram Bot.
- Cấu hình Token & Chat ID tại tab **"Cấu Hình API"**.
