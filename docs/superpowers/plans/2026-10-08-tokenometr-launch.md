# Токенометр: запуск сайта — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** На `https://tokenometr.archik.tech` работают лендинг и вход через Google на archik-kit 0.7.0, а на GitHub выходит Токенометр 1.1.0 с переносом чатов.

**Architecture:**
- Сначала выпускается kit 0.7.0 (дизайн v2 уже в `main`).
- Из его шаблона Copier делается приватный продукт `tokenometr-web`. В нём лендинг в стиле Токенометра, страница «Скоро здесь будет команда» и штатные вход, настройки и деплой kit.
- Продукт выкатывается blue/green на gmn: A-запись на reg.ru через Life OS, `archik-bootstrap`, `make deploy`.

**Tech Stack:** archik-kit 0.7.0 (FastAPI, SQLAlchemy, Alembic, React, Vite), Copier, uv, Node 24, Docker, Postgres 17, gmn (archik-nginx, archik-certificates), reg.ru DNS через Life OS, GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-10-08-tokenometr-teams-design.md` (§3.1, §10, этапы 1–2); kit — `~/Projects/archik-kit/docs/releasing.md`, `deploy/BOOTSTRAP.md`, `deploy/README.md`.

## Global Constraints

- Kit — тег `v0.7.0` по `docs/releasing.md`. GitHub Actions недоступен, поэтому гейты гоняются локально: `make check`, `make check-template`, `node scripts/release.ts check v0.7.0`.
- Ответы шаблона:
  - `product_name="Токенометр"`, `slug=tokenometr`, `domain=tokenometr.archik.tech`;
  - `default_locale=ru`, `layout=topbar`, `pwa=false`;
  - `contact_email=tokenometr@archik.tech`.
- Сайт отвечает только по HTTPS. Адрес возврата Google: `https://tokenometr.archik.tech/api/auth/google/callback`. Клиент Google — `~/Downloads/client_secret_908168982162-iui1v0959l2b014ttf8buee4oi1q6pu0.apps.googleusercontent.com.json`; секрет не печатать и не коммитить.
- DNS: A-запись `tokenometr` → `130.17.12.5` (gmn) в зоне `archik.tech` на reg.ru.
- Свободное место на Mac: тяжёлые шаги (`make check-template`, сборка образа) запускаются через обёртку, которая останавливает их при свободных < 1,5 ГБ.
- Работу в kit веду сам, без субагентов. Документация и PR на русском, код и коммиты на английском, Conventional Commits.
- Каждое изменение на gmn и в DNS: перед шагом короткая строка пользователю, что меняется (владелец одобрил все шаги заранее).

## Review Focus

1. **Адрес возврата Google.** Если запросить начало входа на проде, в адресе Google должен стоять `redirect_uri=https://tokenometr.archik.tech/api/auth/google/callback`. Проверяется в Task 7 через `curl -sI`.
2. **DNS ещё не виден Let's Encrypt.** Если запустить `archik-bootstrap` до распространения записи, сертификат не выпустится. Task 5 ждёт, пока запись не увидят `ns1.reg.ru`, `8.8.8.8` и `1.1.1.1`.
3. **Команда установки на лендинге расходится с README.** Человек копирует её с сайта. Тест в Task 3 сверяет строку с README Токенометра.
4. **Лендинг на телефоне и в тёмной теме.** Барабаны и команда установки не должны вылезать за ширину 375px. Проверяется скриншотами в Task 3.
5. **Диск кончается посреди `make check-template` или сборки образа.** Обёртка из Task 1 останавливает шаг раньше, и Docker не падает.

---

### Task 1: Выпуск archik-kit 0.7.0

**Files:**
- Modify (в worktree `~/Projects/archik-kit-worktrees/release-0.7.0`): версии пакетов через `scripts/release.ts set`, `uv.lock`, `package-lock.json`, `CHANGELOG.md`.
- Create: `$SCRATCH/guard.sh` — обёртка, останавливающая команду при малом свободном месте.

**Interfaces:**
- Produces: тег `v0.7.0`, GitHub Release, npm-пакеты `@icedarold/archik-ui|app|charts@0.7.0` в GitHub Packages, Python-пакет по тегу. Task 2 берёт шаблон `--vcs-ref v0.7.0`.

- [ ] **Step 1: Docker и Postgres kit**

```bash
open -a Docker
until docker info >/dev/null 2>&1; do sleep 2; done
cd ~/Projects/archik-kit && docker compose up --detach --wait
```

Expected: `docker info` отвечает, контейнер Postgres kit healthy.

- [ ] **Step 2: Обёртка от нехватки места**

```bash
cat > "$SCRATCH/guard.sh" <<'EOF'
#!/bin/bash
# Runs a command, killing it if free space on the data volume drops below 1.5 GB.
"$@" & pid=$!
while kill -0 "$pid" 2>/dev/null; do
  free_kb=$(df -k /System/Volumes/Data | awk 'NR==2 {print $4}')
  if [ "$free_kb" -lt 1572864 ]; then
    echo "guard: less than 1.5 GB free, stopping" >&2
    pkill -TERM -P "$pid"; kill -TERM "$pid"; wait "$pid"; exit 99
  fi
  sleep 5
done
wait "$pid"
EOF
chmod +x "$SCRATCH/guard.sh"
```

- [ ] **Step 3: Ветка релиза в worktree**

```bash
cd ~/Projects/archik-kit && git fetch origin
git worktree add ~/Projects/archik-kit-worktrees/release-0.7.0 -b release/v0.7.0 origin/main
cd ~/Projects/archik-kit-worktrees/release-0.7.0
node scripts/release.ts set 0.7.0 && uv lock && npm install
```

- [ ] **Step 4: CHANGELOG**

Перенести «Unreleased» в `## [0.7.0] — 2026-10-08`. Проверить, что у ломающих изменений есть «Как обновиться»:
- утилиты `text-brand`, `border-brand` заменены на `text-brand-strong`, `border-brand-strong`, `text-link`;
- лист страницы убран, вместо него `Card` и `Page`.

Если раздела нет, дописать шаги для продукта: поиск по `text-brand`, `border-brand` и замена; обёртка страниц в `Page`, а блоков — в `Card`.

- [ ] **Step 5: Гейты**

```bash
"$SCRATCH/guard.sh" make check
"$SCRATCH/guard.sh" make check-template
node scripts/release.ts check v0.7.0
```

Expected: все три зелёные. Красный — чинить в этой ветке отдельным коммитом и повторить.

- [ ] **Step 6: PR, мерж, тег**

```bash
git commit -am "chore: release v0.7.0"
git push -u origin release/v0.7.0
gh pr create --title "Релиз 0.7.0: дизайн v2" --body "Дизайн v2 (этап M6): токены, каркас, компоненты для данных, графики, рецепты экранов. Гейты прошли локально: make check, make check-template, release.ts check."
gh pr merge --squash --delete-branch
git fetch origin && git tag v0.7.0 origin/main && git push origin v0.7.0
```

- [ ] **Step 7: Публикация с этой машины**

```bash
gh auth status   # нужен scope write:packages
git checkout v0.7.0 && npm ci
NODE_AUTH_TOKEN=$(gh auth token) node scripts/release.ts publish v0.7.0
```

Expected: GitHub Release `v0.7.0` есть, `npm view @icedarold/archik-ui@0.7.0 version --registry=https://npm.pkg.github.com` печатает `0.7.0`. Если у токена нет `write:packages`, попросить пользователя выполнить `gh auth refresh -h github.com -s write:packages`: это вход через браузер.

### Task 2: Продукт `tokenometr-web` из шаблона

**Files:**
- Create: `~/Projects/tokenometr-web/` (всё из шаблона), `backend/.env` (не в git).

**Interfaces:**
- Consumes: шаблон `v0.7.0`.
- Produces: приватный репозиторий `IceDarold/tokenometr-web`, зелёный `make check` продукта.

- [ ] **Step 1: Сгенерировать**

```bash
uvx copier copy --vcs-ref v0.7.0 --defaults \
  --data product_name="Токенометр" --data slug=tokenometr --data domain=tokenometr.archik.tech \
  --data default_locale=ru --data layout=topbar --data pwa=false --data contact_email=tokenometr@archik.tech \
  https://github.com/IceDarold/archik-kit.git ~/Projects/tokenometr-web
```

- [ ] **Step 2: Репозиторий**

```bash
cd ~/Projects/tokenometr-web && git init -b main && git add -A
git commit -m "chore: start Tokenometr from archik-kit v0.7.0"
gh repo create IceDarold/tokenometr-web --private --source . --push
```

- [ ] **Step 3: Зависимости и `.env` для разработки**

```bash
uv sync
(cd frontend && NODE_AUTH_TOKEN=$(gh auth token) npm install)
cp backend/.env.example backend/.env
```

В `backend/.env`: `SESSION_SECRET` из `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, `PASSWORD_SIGN_IN=true`. Google в разработке не нужен, вход локально — паролем через `archik-kit users add`.

- [ ] **Step 4: Проверка шаблона продукта**

Run: `"$SCRATCH/guard.sh" make check`
Expected: PASS.

### Task 3: Лендинг в стиле Токенометра

**Files:**
- Modify: `public/index.html`, `public/site.css`, `public/privacy.html`, `public/terms.html`, `public/favicon.svg`.
- Create: `public/img/screenshot-light.png`, `public/img/screenshot-dark.png`, `public/img/icon.png` (из `~/Projects/tokenometr/docs/`).
- Test: `backend/tests/test_landing.py`.

**Interfaces:**
- Consumes: README Токенометра (команда установки), скриншоты из `~/Projects/tokenometr/docs/`.

- [ ] **Step 1: Падающий тест на команду установки и ключевые блоки**

```python
# backend/tests/test_landing.py
from pathlib import Path

INSTALL = "curl -fsSL https://raw.githubusercontent.com/IceDarold/tokenometr/main/install.sh | bash"
PUBLIC = Path(__file__).resolve().parents[2] / "public"


def test_landing_offers_the_same_install_command_as_the_readme():
    page = (PUBLIC / "index.html").read_text(encoding="utf-8")
    assert INSTALL in page


def test_landing_says_what_leaves_the_mac_and_links_to_sign_in_and_legal_pages():
    page = (PUBLIC / "index.html").read_text(encoding="utf-8")
    for needle in ('href="/app/"', 'href="/privacy"', 'href="/terms"', "текст переписок"):
        assert needle in page
```

Run: `uv run pytest backend/tests/test_landing.py -v`. Expected: FAIL.

- [ ] **Step 2: Сделать лендинг по скиллу frontend-design**

Загрузить `frontend-design:frontend-design`. Направление — эмаль, барабаны и красные десятые из `~/Projects/tokenometr/web/index.html`: те же токены цвета светлой и тёмной темы, DIN для цифр (`"DIN Alternate", "DIN Condensed"` с запасным Onest и табличными цифрами), Onest шаблона для текста. Порядок блоков — §3.1 спеки. Барабаны крутятся до числа, при `prefers-reduced-motion` стоят сразу. Без внешних шрифтов и скриптов.

- [ ] **Step 3: Тексты privacy и terms**

`privacy.html` перечисляет то, что сайт хранит сейчас: аккаунт Google (почта, имя, аватар), сессии, настройки. Отдельным абзацем — что появится с командами (§4.3 спеки), с пометкой «когда вы подключите Mac к команде». Удаление — через «Удалить аккаунт» в настройках.

- [ ] **Step 4: Тесты и вид**

Run: `uv run pytest backend/tests/test_landing.py -v`. Expected: PASS.

Затем `make dev`. Снять скриншоты `/` во встроенном браузере: 1280px светлая, 1280px тёмная, 375px. На 375px ничего не выходит за ширину, команда переносится или прокручивается внутри своего поля.

- [ ] **Step 5: Коммит**

```bash
git add public backend/tests/test_landing.py && git commit -m "feat: the Tokenometr landing" && git push
```

### Task 4: Каркас приложения без примера Notes

**Files:**
- Modify: `frontend/src/App.tsx`, `frontend/src/Home.tsx`, `frontend/src/Logo.tsx`, `backend/main.py`, `PRODUCT.md`.
- Delete: пример `notes` (бэкенд-модуль, его миграции, страница, тесты) — по разделу шаблона о замене примера в `docs/template.md` kit.

- [ ] **Step 1: Убрать пример, как велит `docs/template.md`.** Тесты продукта и `make openapi` после этого зелёные.
- [ ] **Step 2: Главная `/app`.** «Команды Токенометра скоро появятся здесь» и команда установки в `CopyField`. Logo — SVG из трёх барабанов и красного.
- [ ] **Step 3:** `"$SCRATCH/guard.sh" make check` → PASS; коммит `feat: the Tokenometr shell` и push.

### Task 5: A-запись на reg.ru

- [ ] **Step 1: Сообщить пользователю** «Добавляю A-запись tokenometr.archik.tech → 130.17.12.5 на reg.ru».
- [ ] **Step 2:** `mcp__lifeos__regru_add_dns_record`: зона `archik.tech`, имя `tokenometr`, тип `A`, значение `130.17.12.5`.
- [ ] **Step 3: Ждать распространения**

```bash
for ns in ns1.reg.ru 8.8.8.8 1.1.1.1; do dig +short tokenometr.archik.tech @$ns; done
```

Expected: везде `130.17.12.5`.

### Task 6: Подготовка gmn (`archik-bootstrap`)

**Files (на gmn):** `/etc/tokenometr/deploy.conf`, `/etc/tokenometr/app.env`, сайт в `/etc/archik-sites`, база и роль `tokenometr`, сертификат в `/etc/archik-tls`.

- [ ] **Step 1: Свободная пара портов**

`ssh gmn 'grep -h _PORT /etc/*/deploy.conf'`. Взять ближайшую свободную чётную пару из 18400–18499.

- [ ] **Step 2: Deploy-ключ**

```bash
ssh-keygen -t ed25519 -N '' -C tokenometr-deploy -f ~/.ssh/tokenometr-deploy
scp ~/.ssh/tokenometr-deploy.pub gmn:/root/
```

- [ ] **Step 3: Checkout kit v0.7.0 на gmn**

```bash
git clone --depth 1 --branch v0.7.0 git@github.com:IceDarold/archik-kit.git "$SCRATCH/archik-kit-v0.7.0"
rsync -a "$SCRATCH/archik-kit-v0.7.0/" gmn:/root/archik-kit-v0.7.0/
ssh gmn chown -R root:root /root/archik-kit-v0.7.0
```

- [ ] **Step 4: План bootstrap без `--apply`**

Аргументы по разделу «Запуск» в `BOOTSTRAP.md`: slug `tokenometr`, домен, порты, база и роль `tokenometr`, ключ, `--kit-version v0.7.0`, `--pull-policy missing`. Показать пользователю план одной строкой: что создаётся и что меняется в общих сервисах.

- [ ] **Step 5: `--apply`.** Подтверждения общих сервисов отвечать «да» по одному и сверять с планом.
- [ ] **Step 6: `app.env`**

Файл `/etc/tokenometr/app.env`, права 600:
- `GOOGLE_CLIENT_ID` и `GOOGLE_CLIENT_SECRET` из файла клиента;
- `SESSION_SECRET` — новый, 32 байта;
- `PUBLIC_URL=https://tokenometr.archik.tech`.

Секреты передаются через stdin ssh, а не в аргументах. Остальные переменные — по `deploy/README.md`.

### Task 7: Первый выкат

- [ ] **Step 1:** `"$SCRATCH/guard.sh" docker system df` — проверить, что под образ хватит места.
- [ ] **Step 2: Выкат в фоне, чтобы пережил сессию**

```bash
cd ~/Projects/tokenometr-web
bash -c "set -m; nohup make deploy SERVER=gmn > $SCRATCH/deploy.log 2>&1 & echo \$! > $SCRATCH/deploy.pid"
```

Дальше ждать pid фоновой задачей.

- [ ] **Step 3: Проверки**

```bash
curl -sf https://tokenometr.archik.tech/api/health
curl -s https://tokenometr.archik.tech/ | grep -c "install.sh | bash"
curl -sI "https://tokenometr.archik.tech/api/auth/google/start?return_to=/app/" | grep -i '^location:' | grep -o 'redirect_uri=[^&]*'
```

Expected: health `ok`, команда установки найдена, `redirect_uri=https%3A%2F%2Ftokenometr.archik.tech%2Fapi%2Fauth%2Fgoogle%2Fcallback`. Затем открыть сайт во встроенном браузере, снять скриншот лендинга и экрана входа.

### Task 8: Токенометр 1.1.0 на GitHub

**Files:** всё незакоммиченное в `~/Projects/tokenometr`: перенос чатов, спека, этот план.

- [ ] **Step 1: Коммиты**

Отдельно `feat: keep chats in sync between Claude accounts` (код, тесты, README, версия 1.1.0) и `docs: the design and the launch plan of Tokenometr for teams`.

- [ ] **Step 2: Push и релиз**

```bash
git push
./build.sh
gh release create v1.1.0 build/Tokenometr.zip --title "Токенометр 1.1.0" --notes "Перенос чатов вкладки Code между аккаунтами Claude: фоновая служба держит списки одинаковыми. Подробности — в README, раздел «Чаты на всех аккаунтах»."
```

- [ ] **Step 3: Проверка установщика**

`curl -fsSL https://raw.githubusercontent.com/IceDarold/tokenometr/main/install.sh | bash`. Expected: ставится 1.1.0 (`defaults read ~/Applications/Токенометр.app/Contents/Info CFBundleShortVersionString`), служба переноса работает.
