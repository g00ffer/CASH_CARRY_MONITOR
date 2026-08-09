#!/usr/bin/env bash
# =====================================================================
# Cash-and-Carry Monitor — автоматический деплой на VPS
# Шаги 1–4 одним запуском:
#   1. Подготовка VPS (системные пакеты, Python, NTP)
#   2. Деплой кода и зависимостей
#   3. Конфигурация и секреты
#   4. Systemd service (автозапуск и рестарты)
#
# Использование:
#   chmod +x deploy.sh
#   sudo ./deploy.sh
# =====================================================================

set -euo pipefail

# =====================================================================
# КОНФИГУРАЦИЯ ДЕПЛОЯ
# =====================================================================
APP_NAME="cash-carry-monitor"
APP_USER="monitor"
APP_DIR="/home/${APP_USER}/${APP_NAME}"
VENV_DIR="${APP_DIR}/.venv"
SERVICE_NAME="${APP_NAME}"
PYTHON_VERSION="python3.11"
PYTHON_MIN_VERSION="3.11"

# Путь к локальному проекту (для rsync-варианта)
LOCAL_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# =====================================================================
# ЛОГИРОВАНИЕ
# =====================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
log_step()    { echo -e "\n${BLUE}══════════════════════════════════════════════════${NC}"; echo -e "${BLUE}  $*${NC}"; echo -e "${BLUE}══════════════════════════════════════════════════${NC}"; }

# =====================================================================
# ПРОВЕРКИ
# =====================================================================
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "Скрипт нужно запускать от root: sudo ./deploy.sh"
        exit 1
    fi
}

check_python_version() {
    local python_cmd="$1"
    if ! command -v "$python_cmd" &>/dev/null; then
        return 1
    fi
    local version
    version=$("$python_cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if awk "BEGIN {exit !($version >= $PYTHON_MIN_VERSION)}"; then
        return 0
    fi
    return 1
}

find_python() {
    # Ищем подходящую версию Python
    for py in python3.13 python3.12 python3.11 python3; do
        if check_python_version "$py"; then
            PYTHON_VERSION="$py"
            log_info "Найден Python: $py"
            return 0
        fi
    done
    return 1
}

# =====================================================================
# ШАГ 1. ПОДГОТОВКА VPS
# =====================================================================
step1_system_prepare() {
    log_step "ШАГ 1. Подготовка VPS (системные пакеты, Python, NTP)"

    log_info "Обновление списка пакетов..."
    apt-get update -qq

    log_info "Обновление установленных пакетов..."
    apt-get upgrade -y -qq

    log_info "Установка базовых зависимостей..."
    apt-get install -y -qq \
        "${PYTHON_VERSION}" \
        "${PYTHON_VERSION}-venv" \
        "${PYTHON_VERSION}-dev" \
        python3-pip \
        git \
        curl \
        rsync \
        chrony \
        build-essential \
        libffi-dev \
        libssl-dev

    log_info "Настройка синхронизации времени (NTP)..."
    systemctl enable chrony --quiet 2>/dev/null || true
    systemctl start chrony 2>/dev/null || true

    log_info "Проверка синхронизации времени..."
    timedatectl set-ntp true 2>/dev/null || true
    timedatectl status 2>/dev/null || log_warn "timedatectl недоступен"

    log_info "Шаг 1 завершён."
}

# =====================================================================
# ШАГ 2. ДЕПЛОЙ КОДА И ЗАВИСИМОСТЕЙ
# =====================================================================
step2_deploy_code() {
    log_step "ШАГ 2. Деплой кода и зависимостей"

    # Создаём системного пользователя, если не существует
    if ! id -u "$APP_USER" &>/dev/null; then
        log_info "Создание пользователя ${APP_USER}..."
        useradd -m -s /bin/bash "$APP_USER"
    else
        log_info "Пользователь ${APP_USER} уже существует."
    fi

    # Создаём директорию приложения
    log_info "Создание директории ${APP_DIR}..."
    mkdir -p "$APP_DIR"

    # Копируем код (rsync из текущей директории скрипта)
    log_info "Копирование кода из ${LOCAL_PROJECT_DIR}..."
    rsync -av --delete \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='data/*.sqlite' \
        --exclude='logs/*.log' \
        "${LOCAL_PROJECT_DIR}/" "${APP_DIR}/"

    # Создаём необходимые директории
    log_info "Создание рабочих директорий..."
    mkdir -p "${APP_DIR}/logs" "${APP_DIR}/data"

    # Создаём venv
    log_info "Создание виртуального окружения..."
    if [[ ! -d "$VENV_DIR" ]]; then
        sudo -u "$APP_USER" "$PYTHON_VERSION" -m venv "$VENV_DIR"
    else
        log_info "venv уже существует, обновляем pip..."
    fi

    # Обновляем pip и ставим зависимости
    log_info "Установка Python-зависимостей..."
    sudo -u "$APP_USER" "${VENV_DIR}/bin/pip" install --upgrade pip -q
    sudo -u "$APP_USER" "${VENV_DIR}/bin/pip" install -q \
        ccxt \
        httpx \
        pydantic \
        PyYAML \
        python-dotenv

    # Создаём requirements.txt для будущих обновлений
    if [[ ! -f "${APP_DIR}/requirements.txt" ]]; then
        log_info "Создание requirements.txt..."
        cat > "${APP_DIR}/requirements.txt" << 'EOF'
ccxt>=4.2
httpx>=0.27
pydantic>=2.5
PyYAML>=6.0
python-dotenv>=1.0
EOF
    fi

    log_info "Шаг 2 завершён."
}

# =====================================================================
# ШАГ 3. КОНФИГУРАЦИЯ И СЕКРЕТЫ
# =====================================================================
step3_configuration() {
    log_step "ШАГ 3. Конфигурация и секреты"

    local env_file="${APP_DIR}/.env"

    # Если .env уже существует — не перезаписываем
    if [[ -f "$env_file" ]]; then
        log_info ".env уже существует, пропускаем создание."
    else
        log_info "Создание .env из шаблона..."

        # Проверяем наличие переменных окружения
        local tg_token="${TELEGRAM_BOT_TOKEN:-}"
        local tg_chat="${TELEGRAM_CHAT_ID:-}"

        if [[ -z "$tg_token" || -z "$tg_chat" ]]; then
            log_warn "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы в окружении."
            log_warn "Создаём .env с заглушками. Заполните вручную!"
        fi

        cat > "$env_file" << EOF
# =====================================================================
# Telegram secrets
# =====================================================================
TELEGRAM_BOT_TOKEN=${tg_token:-ЗАПОЛНИТЕ_ТОКЕН}
TELEGRAM_CHAT_ID=${tg_chat:-ЗАПОЛНИТЕ_CHAT_ID}

# =====================================================================
# Binance API keys (не обязательны для Stage 1)
# =====================================================================
BINANCE_API_KEY=
BINANCE_API_SECRET=

# =====================================================================
# Optional runtime overrides
# =====================================================================
MONITOR_ENVIRONMENT=prod
MONITOR_SETTINGS_PATH=${APP_DIR}/config/settings.yaml
MONITOR_SYMBOLS_PATH=${APP_DIR}/config/symbols.yaml
EOF

        log_warn "⚠️  Не забудьте отредактировать ${env_file}!"
    fi

    # Права на .env — только для владельца
    chmod 600 "$env_file"
    chown "${APP_USER}:${APP_USER}" "$env_file"

    # Права на рабочие директории
    chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/logs" "${APP_DIR}/data"

    # Проверяем наличие конфигов
    if [[ ! -f "${APP_DIR}/config/settings.yaml" ]]; then
        log_error "config/settings.yaml не найден!"
        exit 1
    fi
    if [[ ! -f "${APP_DIR}/config/symbols.yaml" ]]; then
        log_error "config/symbols.yaml не найден!"
        exit 1
    fi

    log_info "Шаг 3 завершён."
}

# =====================================================================
# ШАГ 4. SYSTEMD SERVICE
# =====================================================================
step4_systemd() {
    log_step "ШАГ 4. Настройка Systemd (автозапуск и рестарты)"

    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"

    log_info "Создание systemd unit: ${service_file}..."
    cat > "$service_file" << EOF
[Unit]
Description=Cash-and-Carry Monitor (Stage 1)
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment="PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=${APP_DIR}/.env
ExecStart=${VENV_DIR}/bin/python -m monitor.main

# Рестарт при любых падениях, кроме ручного останова
Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5

# Лимиты и безопасность
StandardOutput=journal
StandardError=journal
SyslogIdentifier=monitor

# Ресурсные лимиты
MemoryMax=512M
CPUQuota=50%

# Безопасность (hardening)
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${APP_DIR}/data ${APP_DIR}/logs

[Install]
WantedBy=multi-user.target
EOF

    log_info "Перезагрузка systemd daemon..."
    systemctl daemon-reload

    log_info "Включение автозапуска..."
    systemctl enable "$SERVICE_NAME" --quiet

    log_info "Шаг 4 завершён."
}

# =====================================================================
# ФИНАЛЬНАЯ ПРОВЕРКА И ЗАПУСК
# =====================================================================
final_check_and_start() {
    log_step "ФИНАЛЬНАЯ ПРОВЕРКА И ЗАПУСК"

    # Проверяем, что .env заполнен
    local env_file="${APP_DIR}/.env"
    if grep -q "ЗАПОЛНИТЕ_ТОКЕН" "$env_file" 2>/dev/null; then
        log_warn "⚠️  .env содержит заглушки. Сервис не запускается."
        log_warn "    Отредактируйте ${env_file} и выполните:"
        log_warn "    sudo systemctl start ${SERVICE_NAME}"
        return 0
    fi

    # Проверяем конфиг через Python (dry-run валидация)
    log_info "Валидация конфигурации..."
    if sudo -u "$APP_USER" bash -c "cd ${APP_DIR} && PYTHONPATH=src ${VENV_DIR}/bin/python -c \"from monitor.config import load_settings; s = load_settings('${APP_DIR}/config/settings.yaml', '${APP_DIR}/config/symbols.yaml'); print(f'Config OK: {len(s.symbols)} symbols')\""; then
        log_info "Конфигурация валидна."
    else
        log_error "Ошибка валидации конфигурации! Проверьте settings.yaml и symbols.yaml."
        exit 1
    fi

    # Запускаем сервис
    log_info "Запуск сервиса ${SERVICE_NAME}..."
    systemctl restart "$SERVICE_NAME"

    # Ждём немного и проверяем статус
    sleep 3
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        log_info "✅ Сервис запущен и работает!"
    else
        log_error "❌ Сервис не запустился. Проверьте логи:"
        log_error "   journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
        exit 1
    fi

    # Выводим итоговую информацию
    echo ""
    log_step "ДЕПЛОЙ ЗАВЕРШЁН УСПЕШНО"
    echo ""
    echo "  Сервис:      ${SERVICE_NAME}"
    echo "  Директория:  ${APP_DIR}"
    echo "  Пользователь: ${APP_USER}"
    echo "  Логи:        journalctl -u ${SERVICE_NAME} -f"
    echo "  Статус:      systemctl status ${SERVICE_NAME}"
    echo "  Остановка:   systemctl stop ${SERVICE_NAME}"
    echo "  Перезапуск:  systemctl restart ${SERVICE_NAME}"
    echo ""
}

# =====================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# =====================================================================
main() {
    echo ""
    log_step "🚀 Cash-and-Carry Monitor — Автодеплой"
    echo ""

    check_root
    find_python || { log_error "Python >= ${PYTHON_MIN_VERSION} не найден!"; exit 1; }

    step1_system_prepare
    step2_deploy_code
    step3_configuration
    step4_systemd
    final_check_and_start
}

main "$@"