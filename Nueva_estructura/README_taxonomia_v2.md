# Taxonomía v2 + atributos — cómo aplicarla

Nada de esto borra productos, variantes ni ventas. Cada paso que escribe tiene
modo simulación por defecto.

## Archivos

| Archivo | Qué es |
|---|---|
| `kawii-pluss-v1.json` | Taxonomía actual (export de `GET /taxonomy/export`) |
| `migracion_v1_v2.json` | 128 operaciones rename/move/merge/create/drop_empty v1 → v2 |
| `kawii-pluss-v2.json` | Resultado (`python -m tools.taxonomy.build_v2`) |
| `driver_compras_v2.xlsx` | Plantilla del driver con columnas de atributos y listas |
| `reportes/` | CSV generados por los scripts (separador `;`, se abren en Excel) |

## Orden de pasos (empresa 1 = Kawii Pluss)

1. **Migración de BD** (tablas de atributos y proveedores, vista `v_products_enriched`):
   ```
   psql ... -f docs/migrations/2026-11-02_product_attributes.sql
   ```
2. **Inferir atributos ANTES de migrar** (la v2 junta tipos y el dato pasa al atributo):
   ```
   python -m tools.taxonomy.infer_attributes --company-id 1          # vista previa
   python -m tools.taxonomy.infer_attributes --company-id 1 --apply  # guarda (source=inferred)
   ```
3. **Migrar la taxonomía** (simulación → confirmar):
   ```
   python -m tools.taxonomy.migrate --company-id 1
   python -m tools.taxonomy.migrate --company-id 1 --apply
   ```
   Equivale a `POST /taxonomy/migrate` con `{"ops": [...], "dry_run": true}`.
   Aborta si cambia el número de productos, productos clasificados, tipos o metas.
4. **Revisar y aplicar reclasificaciones**:
   ```
   python -m tools.taxonomy.build_reclassification --company-id 1
   ```
   Abrir `reportes/reclasificacion_v2.csv`, poner `SI` en la columna `aplicar`
   de las filas correctas y luego:
   ```
   python -m tools.taxonomy.apply_reclassification --company-id 1          # simulación
   python -m tools.taxonomy.apply_reclassification --company-id 1 --apply  # override interno
   python -m tools.taxonomy.infer_attributes --company-id 1 --apply        # recalcula lo inferido
   ```
   Volver a inferir actualiza los atributos que salían de la clasificación vieja
   (ej. público de un perfume que estaba mal en "Hombre"). No toca lo cargado a mano ni por driver.
5. **BSale** (opcional, con aprobación): renombrar tipos con
   `reportes/bsale_renombres_sugeridos.csv` vía `POST /bsale/product-types/bulk-map`
   (`rename_in_bsale=true`) y mapear los tipos de `reportes/mapeo_tipos_v2.csv`.
   Ningún tipo se borra.
6. **Driver**: usar `driver_compras_v2.xlsx` (o `GET /attributes/driver-template`)
   e importar con `POST /attributes/import-driver?dry_run=true` → revisar → `dry_run=false`.
7. **Nombres de variante en BSale** (fase 2):
   ```
   python -m tools.taxonomy.push_variant_attributes --company-id 1 --nombres          # simulación
   python -m tools.taxonomy.push_variant_attributes --company-id 1 --nombres --apply
   ```

## Dónde se ve

- `GET /compras/ecosistema?nivel=marca|proveedor|licencia|origen|publico|temporada|estilo|material`
- `GET /analytics/reporte-ventas` → `por_atributo` + hoja "Por atributo" en el Excel
- `GET /attributes/coverage` → % de productos con cada atributo
