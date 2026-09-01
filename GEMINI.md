# Workspace Rules & Guidelines

## Freqtrade Bot Integration & MCP Connector
- **Всегда использовать MCP коннектор (nfi_mcp / Freqtrade MCP tools / API), когда речь идет о мониторинге, получении статуса, управлении, сделках, сигналах или анализе бота.**
- Для взаимодействия с удаленными ботами (nfi на порту 8087, sample на порту 8086) и их API/сигналами использовать MCP сервер nfi_mcp / ssh-коннектор.
