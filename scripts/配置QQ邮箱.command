#!/bin/zsh
cd "${0:A:h:h}"
.venv/bin/python -m paper_digest.weekly setup-qq
printf '\n按回车关闭窗口。'
read
