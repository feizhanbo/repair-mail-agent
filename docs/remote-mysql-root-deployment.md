# Remote MySQL Root Deployment Record

This record intentionally stores only non-sensitive deployment facts. Do not write the root password in this file.

## Deployment Facts

| Item | Value |
| --- | --- |
| Database account | `root` |
| Database name | `repair_system_dev` |
| Container name | `repair-mysql` |
| Image | `mysql:8.0` |
| Compose network | `repair_net` |
| Actual Docker network | `repair-mail-agent_repair_net` |
| Compose volume | `mysql_data` |
| Actual Docker volume | `repair-mail-agent_mysql_data` |
| Container data path | `/var/lib/mysql` |
| Remote project directory | `/root/bert/repair-mail-agent` |
| Host port binding | `127.0.0.1:3307:3306` |
| Local tunnel | `127.0.0.1:13307 -> remote 127.0.0.1:3307` |
| Character set | `utf8mb4` |
| Collation | `utf8mb4_unicode_ci` |
| Time zone | `+08:00` |

## Password Handling

- Save the real root password immediately in the remote server project `.env`.
- Save the same password in the local private `.env` used for SSH-tunnel development.
- Save the same password in the user's password manager or offline secure record.
- Keep `.env` and `.env.*` out of Git. The repository only keeps `.env.example` with placeholder values.
- Restrict the remote `.env` file to the deployment user, for example with `chmod 600 .env`.
- Quote `.env` values that contain spaces, for example `DEFAULT_ADMIN_REAL_NAME="System Administrator"`, so Bash-based deployment scripts can safely `source .env`.

## Remote Pre-Deployment Checks

Run on the remote server before starting MySQL:

```bash
docker --version
docker compose version
git --version
df -h
free -h
date
ss -lntp | grep -E '3306|3307' || true
docker ps -a --filter name=repair-mysql
docker volume ls | grep mysql_data || true
docker network ls | grep repair_net || true
```

Confirm that MySQL ports `3306` and `3307` are not open to the public network. The compose file binds the new project MySQL to remote `127.0.0.1:3307` only.

## Remote Startup

Create the remote private `.env` from `.env.example`, replace `MYSQL_ROOT_PASSWORD`, and keep the same value in `DATABASE_URL`.
If the root password contains special characters, URL-encode it in `DATABASE_URL`.

```bash
docker compose up -d mysql
docker compose ps
docker logs repair-mysql
```

## Remote Validation

Use interactive password input so the password is not written into shell history:

```bash
docker exec -it repair-mysql mysql -uroot -p repair_system_dev
```

Inside MySQL:

```sql
SHOW DATABASES;
SHOW VARIABLES LIKE 'character_set_server';
SHOW VARIABLES LIKE 'collation_server';
SELECT @@time_zone;
CREATE TABLE IF NOT EXISTS smoke_test_root_mysql (id INT PRIMARY KEY, note VARCHAR(50));
INSERT INTO smoke_test_root_mysql (id, note) VALUES (1, 'ok')
ON DUPLICATE KEY UPDATE note = VALUES(note);
SELECT * FROM smoke_test_root_mysql;
DROP TABLE smoke_test_root_mysql;
```

## Local Tunnel Validation

Start the SSH tunnel from the local machine:

```powershell
ssh -N -L 13307:127.0.0.1:3307 <server-alias>
```

The local private `.env` should use:

```text
DATABASE_URL=mysql+asyncmy://root:<ROOT_PASSWORD>@127.0.0.1:13307/repair_system_dev
```

URL-encode special characters in `<ROOT_PASSWORD>` when writing the connection URL.

Do not commit that local `.env`.

## Migration, Seed, And Smoke Validation

After the remote MySQL container is healthy and the local tunnel is active, run from the local backend directory:

```bash
alembic upgrade head
python -m app.seed
python -m app.db_smoke
```

Expected smoke results for the current baseline:

```text
smoke: ok
alembic: 0f2ae6ba263f
tables: 27
workflow_statuses: 8
workflow_transitions: 16
roles: 4
users: 1
reply_templates: 3
```

`tables: 27` means 26 business tables plus `alembic_version`.

To validate remote-host-to-container access without exposing MySQL publicly, use a MySQL client on the remote host or a temporary client container against `127.0.0.1:3307`, with the password read from the remote private `.env`.

## Git-Based Update Flow

After a private GitHub repository exists:

```bash
git remote add origin <private-repo-url>
git push -u origin main
```

On the remote server, keep `/root/bert/repair-mail-agent/.env` local-only, then update application code by pulling the private repository or by CI/CD. Never replace the remote `.env` with the tracked `.env.example`.
