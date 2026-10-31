# AGENTS.md — Directrices para Agentes de IA en HUDED Backend

## 1. Grafo de Conocimiento del Proyecto (Graphify)
Este repositorio cuenta con un grafo de conocimiento generado en `graphify-out/` que indexa AST, dependencias, rutas, routers, servicios y modelos.

### Reglas obligatorias para la IA:
1. **Consultar Graphify primero**: Ante cualquier pregunta o tarea sobre arquitectura, flujo de ejecución, relaciones entre módulos o ubicación de funciones/clases:
   - Usa `graphify query "<término o pregunta>"` para encontrar módulos y funciones relevantes.
   - Usa `graphify path "<Origen>" "<Destino>"` (o con `--undirected`) para rastrear dependencias directas e indirectas.
   - Usa `graphify explain "<símbolo>"` para inspeccionar dependencias y llamantes de un componente clave.
   - Consulta `graphify-out/GRAPH_REPORT.md` para entender los *God Nodes* y las comunidades del sistema.
2. **Actualizar el grafo tras modificar código**:
   - Tras crear, editar o eliminar módulos de código Python, ejecuta siempre:
     ```bash
     graphify update .
     ```
   - Este comando actualiza el AST localmente en segundos sin costo de tokens de API.

## 2. Multi-tenancy y Seguridad
- El proyecto opera bajo un modelo multi-empresa con contexto de tenant (`CurrentCompany`, `current_company_id()`) y filtrado por roles (`CurrentUser`, `require_role`).
- Respeta siempre el scope de oficina (`office_scope_ids()`) y el Row-Level Security (RLS) en consultas a PostgreSQL.
