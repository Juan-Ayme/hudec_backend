---
trigger: always_on
description: Consult the graphify knowledge graph at graphify-out/ for codebase and architecture questions.
---

# Graphify Knowledge Graph & Architecture Rules

Este proyecto cuenta con un grafo de conocimiento de código y arquitectura generado en `graphify-out/`.

## Instrucciones para el Asistente de IA (Antigravity / Gemini)

### 1. Prioridad de consulta de arquitectura
Antes de realizar búsquedas manuales ciegas (`grep`, búsquedas de texto plano masivas o lectura de múltiples archivos no relacionados) para responder dudas sobre:
- Arquitectura general y diseño del sistema
- Relaciones, dependencias o imports entre módulos
- Flujo de datos, routers, servicios o modelos
- Localización de funciones, clases o abstracciones centrales

**DEBES utilizar Graphify como primera opción:**
- **Relaciones entre componentes:** Ejecuta `graphify path "<A>" "<B>"` (o `graphify path "<A>" "<B>" --undirected`) para ver el camino más corto de dependencias.
- **Entender un símbolo o módulo:** Ejecuta `graphify explain "<concepto>"` para obtener el nodo y sus conexiones inmediatas.
- **Búsqueda contextual de código:** Ejecuta `graphify query "<término o pregunta>"` (CLI) o usa las herramientas de MCP `query_graph` / `shortest_path` / `get_node` si están disponibles.
- **Visión global de arquitectura:** Revisa `graphify-out/GRAPH_REPORT.md` para entender hubs principales (*God Nodes*), comunidades y módulos del sistema.

### 2. Mantenimiento del Grafo
Siempre que modifiques, agregues o refactorices archivos de código en este repositorio durante la sesión:
- Ejecuta `graphify update .` al terminar los cambios de código. Es 100% determinista vía AST local, rápido y no consume cuota de API.
