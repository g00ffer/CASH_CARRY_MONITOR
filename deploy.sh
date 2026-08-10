#!/usr/bin/env bash
# =====================================================================
# Cash-and-Carry Monitor — автоматический деплой на VPS
# Использование:
#   chmod +x deploy.sh
#   sudo ./deploy.sh
# =====================================================================

set -euo pipefail

# =====================================================================
# КОНФИГУРАЦИЯ ДЕПЛОЯ
# =====================================================================
APP_NAME="cash-carry-monitor"
APP_USER="gooffer"                              # <-- ИСПРАВЛЕНО: было "monitor"
APP_DIR="/home/${APP_USER}/${APP_NAME}"
VENV_DIR="${APP_DIR}/.venv"
SERVICE_NAME="${APP_NAME}"
PYTHON_MIN_VERSION="3.11"

LOCAL_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# =====================================================================
# ЛОГИРОВАНИЕ
# =====================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

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
    log_step "ШАГ 1. Подготовка VPS"

    log_info "Обновление списка пакетов..."
    apt-get update -qq

    log_info "Установка базовых зависимостей..."
    apt-get install -y -qq \
        "${PYTHON_VERSION}" \
        "${PYTHON_VERSION}-venv" \
        "${PYTHON_VERSION}-dev" \
        python3-pip \
        git curl rsync chrony \
        build-essential libffi-dev libssl-dev

    log_info "Настройка NTP..."
    systemctl enable chrony --quiet 2>/dev/null || true
    systemctl start chrony 2>/dev/null || true
    timedatectl set-ntp true 2>/dev/null || true

    log_info "Шаг 1 завершён."
}

# =====================================================================
# ШАГ 2. ДЕПЛОЙ КОДА И ЗАВИСИМОСТЕЙ
# =====================================================================
step2_deploy_code() {
    log_step "ШАГ 2. Деплой кода и зависимостей"

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

    log_info "Создание рабочих директорий..."
    mkdir -p "${APP_DIR}/logs" "${APP_DIR}/data"

    log_info "Создание/обновление venv..."
    if [[ ! -d "$VENV_DIR" ]]; then
        sudo -u "$APP_USER" "$PYTHON_VERSION" -m venv "$VENV_DIR"
    fi

    log_info "Установка Python-зависимостей..."
    sudo -u "$APP_USER" "${VENV_DIR}/bin/pip" install --upgrade pip -q

    if [[ -f "${APP_DIR}/requirements.txt" ]]; then
        sudo -u "$APP_USER" "${VENV_DIR}/bin/pip" install -q -r "${APP_DIR}/requirements.txt"
    else
        sudo -u "$APP_USER" "${VENV_DIR}/bin/pip" install -q \
            ccxt httpx pydantic PyYAML python-dotenv
    fi

    chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"
    log_info "Шаг 2 завершён."
}

# =====================================================================
# ШАГ 3. КОНФИГУРАЦИЯ И СЕКРЕТЫ
# =====================================================================
step3_configuration() {
    log_step "ШАГ 3. Конфигурация и секреты"

    local env_file="${APP_DIR}/.env"

    if [[ -f "$env_file" ]]; then
        log_info ".env уже существует, пропускаем создание."
    else
        log_warn "⚠️  .env не найден! Создаём с заглушками."
        cat > "$env_file" << EOF
TELEGRAM_BOT_TOKEN=ЗАПОЛНИТЕ_ТОКЕН
TELEGRAM_CHAT_ID=ЗАПОЛНИТЕ_CHAT_ID
BINANCE_API_KEY=
BINANCE_API_SECRET=
MONITOR_ENVIRONMENT=prod
EOF
        log_warn "⚠️  Отредактируйте ${env_file}!"
    fi

    chmod 600 "$env_file"
    chown "${APP_USER}:${APP_USER}" "$env_file"
    chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/logs" "${APP_DIR}/data"

    log_info "Шаг 3 завершён."
}

# =====================================================================
# ШАГ 4. SYSTEMD SERVICE
# =====================================================================
step4_systemd() {
    log_step "ШАГ 4. Настройка Systemd"

    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"

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
Environment="PYTHONPATH=${APP_DIR}/src"
EnvironmentFile=${APP_DIR}/.env
ExecStart=${VENV_DIR}/bin/python -m monitor.main
Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=monitor
MemoryMax=512M
CPUQuota=50%
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${APP_DIR}/data ${APP_DIR}/logs

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" --quiet
    log_info "Шаг 4 завершён."
}

# =====================================================================
# ФИНАЛЬНАЯ ПРОВЕРКА И ЗАПУСК
# =====================================================================
final_check_and_start() {
    log_step "ФИНАЛЬНАЯ ПРОВЕРКА И ЗАПУСК"

    local env_file="${APP_DIR}/.env"
    if grep -q "ЗАПОЛНИТЕ_ТОКЕН" "$env_file" 2>/dev/null; then
        log_warn "⚠️  .env содержит заглушки. Сервис не запускается."
        log_warn "    Отредактируйте ${env_file} и выполните:"
        log_warn "    sudo systemctl start ${SERVICE_NAME}"
        return 0
    fi

    log_info "Валидация конфигурации..."
    if sudo -u "$APP_USER" bash -c "cd ${APP_DIR} && PYTHONPATH=src ${VENV_DIR}/bin/python -c \"from monitor.config import load_settings; s = load_settings('${APP_DIR}/config/settings.yaml', '${APP_DIR}/config/symbols.yaml'); print(f'Config OK: {len(s.symbols)} symbols')\""; then
        log_info "Конфигурация валидна."
    else
        log_error "Ошибка валидации конфигурации!"
        exit 1
    fi

    log_info "Запуск сервиса..."
    systemctl restart "$SERVICE_NAME"
    sleep 3

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        log_info "✅ Сервис запущен и работает!"
    else
        log_error "❌ Сервис не запустился. Проверьте логи:"
        log_error "   journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
        exit 1
    fi

    echo ""
    log_step "ДЕПЛОЙ ЗАВЕРШЁН УСПЕШНО"
    echo ""
    echo "  Сервис:       ${SERVICE_NAME}"
    echo "  Директория:   ${APP_DIR}"
    echo "  Логи:         journalctl -u ${SERVICE_NAME} -f"
    echo "  Перезапуск:   sudo systemctl restart ${SERVICE_NAME}"
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