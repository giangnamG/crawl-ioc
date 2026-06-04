# Deploy 2 VPS

Mục tiêu của thư mục này:

- VPS app: chạy backend Flask/Gunicorn và Nginx.
- VPS DB: chạy PostgreSQL riêng, expose port có kiểm soát cho VPS app.
- Backend connect tới DB qua `POSTGRES_HOST` và `POSTGRES_PORT`.
- Import dữ liệu SQLite cũ sang PostgreSQL production bằng script có reset sequence.

## 1. Sinh env mạnh

Chạy một lần ở máy có repo, thay `<DB_VPS_IP>` bằng IP private nếu 2 VPS cùng private network. Nếu không có private network thì dùng public IP và bắt buộc firewall chỉ cho VPS app truy cập port PostgreSQL.

```bash
cd deploy2
chmod +x scripts/*.sh postgres/init/*.sh
./scripts/generate-secrets.sh --postgres-host <DB_VPS_IP>
```

Script tạo:

```text
deploy2/db.env   -> copy sang VPS PostgreSQL
deploy2/app.env  -> copy sang VPS backend/nginx
```

`POSTGRES_APP_USER`, `POSTGRES_APP_PASSWORD`, admin password, `SECRET_KEY` và Basic Auth password đều được sinh ngẫu nhiên. `validate-env.sh` sẽ chặn password yếu hoặc placeholder.

## 2. Deploy PostgreSQL trên VPS DB

Copy repo hoặc ít nhất thư mục `deploy2` sang VPS DB, đặt `db.env` cạnh `docker-compose.db.yml`, rồi chạy:

```bash
cd deploy2
chmod +x scripts/*.sh postgres/init/*.sh
./scripts/deploy-db.sh --logs
```

Firewall khuyến nghị:

```bash
sudo ufw allow from <APP_VPS_IP> to any port 5432 proto tcp
sudo ufw deny 5432/tcp
```

Nếu muốn bind Postgres vào private interface thay vì mọi interface, sửa trong `db.env`:

```text
POSTGRES_BIND_ADDRESS=<DB_PRIVATE_IP>
POSTGRES_PORT=5432
```

## 3. Deploy backend + Nginx trên VPS app

Copy repo sang VPS app, đặt `app.env` trong `deploy2`, kiểm tra `POSTGRES_HOST` và `POSTGRES_PORT`, rồi chạy:

```bash
cd deploy2
chmod +x scripts/*.sh
./scripts/deploy-app.sh --logs
```

Nginx publish `HTTP_PORT` ra host. Backend chỉ expose trong Docker network và dùng:

```text
DB_BACKEND=postgresql
POSTGRES_HOST=<DB_VPS_IP>
POSTGRES_PORT=5432
POSTGRES_DB=ioc_investigator
POSTGRES_USER=<POSTGRES_APP_USER>
POSTGRES_PASSWORD=<POSTGRES_APP_PASSWORD>
```

## 4. Import dữ liệu SQLite cũ sang PostgreSQL mới

Nên import trước lần chạy app production đầu tiên, hoặc dừng app trước khi import để tránh ghi đồng thời.

Trên VPS app, đặt file SQLite cũ ở một đường dẫn an toàn, ví dụ:

```text
/opt/88i/import/ioc_investigator.sqlite3
```

Chạy import qua backend image để dùng đúng dependency của app:

```bash
cd deploy2
./scripts/import-sqlite-to-postgres.sh \
  --sqlite-path /opt/88i/import/ioc_investigator.sqlite3 \
  --replace
```

`--replace` sẽ truncate các bảng app trong PostgreSQL rồi import toàn bộ bảng có trong SQLite cũ. Script cũng reset sequence Postgres theo `MAX(id)` để dữ liệu mới tạo sau import không bị trùng ID.

Wrapper mount cả thư mục chứa SQLite để đọc được các file sidecar `*.sqlite3-wal` và `*.sqlite3-shm` nếu database cũ đang dùng WAL. Script Python vẫn mở SQLite ở chế độ read-only.

Sau import:

```bash
./scripts/deploy-app.sh --logs
```

## 5. Kiểm tra nhanh

```bash
curl -I http://<APP_VPS_IP>/
curl -I -u admin:'<NGINX_BASIC_AUTH_PASSWORD>' http://<APP_VPS_IP>/
curl -I http://<APP_VPS_IP>/.env
```

Kỳ vọng:

```text
/      không auth: 401 Unauthorized
/      có auth:   200 OK hoặc 302 Found
/.env  luôn 404 Not Found
```

## File chính

```text
docker-compose.db.yml
docker-compose.app.yml
Dockerfile.backend
Dockerfile.nginx
db.env.example
app.env.example
postgres/init/01-create-app-user.sh
scripts/generate-secrets.sh
scripts/validate-env.sh
scripts/deploy-db.sh
scripts/deploy-app.sh
scripts/import-sqlite-to-postgres.sh
scripts/import_sqlite_to_postgres.py
```
