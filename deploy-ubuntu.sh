#!/usr/bin/env bash
# =============================================================
#  Infinite-Canvas-N — Ubuntu 24.04 一键部署脚本
#  用法：chmod +x deploy-ubuntu.sh && ./deploy-ubuntu.sh
# =============================================================
set -euo pipefail

# ---------- 可配置项 ----------
APP_DIR="${HOME}/Infinite-Canvas-N"
APP_PORT=3000
SERVICE_NAME="infinite-canvas"
# ------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ---------- 0. 前置检查 ----------
check_root() {
    if [[ $EUID -eq 0 ]]; then
        error "请不要用 root 运行此脚本，用普通用户即可（systemd 会用 sudo 提权安装服务）。"
        exit 1
    fi
}

check_ubuntu() {
    if ! grep -qi "ubuntu" /etc/os-release 2>/dev/null; then
        warn "此脚本针对 Ubuntu 编写，当前系统可能不完全兼容。继续执行..."
    fi
}

# ---------- 1. 安装系统依赖 ----------
install_deps() {
    info "更新软件源..."
    sudo apt-get update -qq

    info "安装 Python3 + pip + venv + git..."
    sudo apt-get install -y -qq python3 python3-pip python3-venv git

    PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
    info "Python 版本：$(python3 --version)"
}

# ---------- 2. 克隆 / 更新项目 ----------
setup_project() {
    if [[ -d "${APP_DIR}/.git" ]]; then
        info "项目目录已存在，拉取最新代码..."
        cd "${APP_DIR}" && git pull
    else
        if [[ -d "${APP_DIR}" ]]; then
            warn "${APP_DIR} 已存在但不是 git 仓库，跳过克隆。"
        else
            info "克隆项目..."
            git clone https://github.com/hero8152/Infinite-Canvas-N.git "${APP_DIR}"
        fi
    fi
}

# ---------- 3. 创建虚拟环境 & 安装依赖 ----------
setup_venv() {
    cd "${APP_DIR}"

    if [[ ! -d "${APP_DIR}/venv" ]]; then
        info "创建 Python 虚拟环境..."
        python3 -m venv "${APP_DIR}/venv"
    fi

    info "激活虚拟环境并安装依赖..."
    source "${APP_DIR}/venv/bin/activate"
    pip install --upgrade pip -q
    if [[ -f "${APP_DIR}/requirements.txt" ]]; then
        pip install -r "${APP_DIR}/requirements.txt" -q
    else
        warn "未找到 requirements.txt，尝试手动安装核心依赖..."
        pip install fastapi uvicorn requests pydantic python-multipart httpx pillow -q
    fi
    deactivate
}

# ---------- 4. 创建 systemd 服务 ----------
install_service() {
    local USER_NAME=$(whoami)
    local PYTHON_BIN="${APP_DIR}/venv/bin/python"
    local WORK_DIR="${APP_DIR}"

    info "创建 systemd 服务：${SERVICE_NAME}.service"

    sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Infinite-Canvas-N Web Application
After=network.target

[Service]
Type=simple
User=${USER_NAME}
Group=${USER_NAME}
WorkingDirectory=${WORK_DIR}
ExecStart=${PYTHON_BIN} ${WORK_DIR}/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

# 安全限制
NoNewPrivileges=true
ProtectSystem=false

# 日志输出给 journald（systemctl logs 可查）
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}"
    info "服务已创建并设置为开机自启。"
}

# ---------- 5. 启动服务 ----------
start_service() {
    sudo systemctl restart "${SERVICE_NAME}"
    sleep 2

    if sudo systemctl is-active --quiet "${SERVICE_NAME}"; then
        info "✅ 服务启动成功！"
        info "   访问地址：http://127.0.0.1:${APP_PORT}/"
    else
        error "❌ 服务启动失败，查看日志："
        error "   sudo journalctl -u ${SERVICE_NAME} -n 30 --no-pager"
        exit 1
    fi
}

# ---------- 6. 配置防火墙（可选） ----------
config_firewall() {
    if command -v ufw &>/dev/null && sudo ufw status | grep -q "active"; then
        read -rp "检测到 ufw 防火墙已启用，是否放行端口 ${APP_PORT}？[y/N] " answer
        if [[ "${answer}" =~ ^[Yy]$ ]]; then
            sudo ufw allow "${APP_PORT}/tcp"
            info "已放行端口 ${APP_PORT}/tcp"
        fi
    fi
}

# ---------- 完成 ----------
print_summary() {
    echo ""
    echo "============================================"
    echo -e "  ${GREEN}Infinite-Canvas-N 部署完成！${NC}"
    echo "============================================"
    echo ""
    echo "  🌐 访问地址：http://127.0.0.1:${APP_PORT}/"
    echo ""
    echo "  常用命令："
    echo "    启动服务：   sudo systemctl start ${SERVICE_NAME}"
    echo "    停止服务：   sudo systemctl stop ${SERVICE_NAME}"
    echo "    重启服务：   sudo systemctl restart ${SERVICE_NAME}"
    echo "    查看状态：   sudo systemctl status ${SERVICE_NAME}"
    echo "    查看日志：   sudo journalctl -u ${SERVICE_NAME} -f"
    echo "    更新项目：   cd ${APP_DIR} && git pull && sudo systemctl restart ${SERVICE_NAME}"
    echo ""
    echo "  日志说明："
    echo "    系统日志：  /var/log/syslog 或 journalctl -u ${SERVICE_NAME}"
    echo "    应用日志：  ${APP_DIR}/data/logs/server.log（自动按天轮转，保留 7 天）"
    echo ""
    echo "  如果需要外网访问，请配置 Nginx 反向代理。"
    echo "============================================"
}

# ---------- 主流程 ----------
main() {
    check_root
    check_ubuntu
    install_deps
    setup_project
    setup_venv
    install_service
    start_service
    config_firewall
    print_summary
}

main "$@"
