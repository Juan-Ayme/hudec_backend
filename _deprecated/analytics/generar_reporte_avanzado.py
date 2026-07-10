import psycopg2
import pandas as pd
import warnings
import os

# Ignorar la advertencia de SQLAlchemy
warnings.filterwarnings('ignore')

# Definir rutas
ruta_sql = r'c:\Users\juana\Documents\Kawii_analisis_datos\backend_kawii\produccion\consultas\estadistica_logistica_avanzada.sql'
ruta_excel = r'c:\Users\juana\Documents\Kawii_analisis_datos\backend_kawii\produccion\analytics\Reporte_Logistico_Avanzado.xlsx'

def generar_reporte():
    print("⏳ Conectando a la base de datos...")
    conn = psycopg2.connect(
        dbname="database_kawii_pluss",
        user="postgres",
        password="root",
        host="localhost",
        port="5432"
    )

    # 1. Leer el archivo SQL
    with open(ruta_sql, 'r', encoding='utf-8') as file:
        query = file.read()

    print("🚀 Ejecutando la Inteligencia de Inventarios (puede tardar unos segundos)...")
    
    # 2. Cargar los datos a un DataFrame de Pandas
    df_detalle = pd.read_sql(query, conn)
    
    # Rellenar categorías vacías para que no de error
    df_detalle['Categoría'] = df_detalle['Categoría'].fillna('Sin Categoría')
    df_detalle['Subcategoría'] = df_detalle['Subcategoría'].fillna('Sin Subcategoría')

    print(f"✅ Se analizaron {len(df_detalle)} productos. Construyendo reportes agrupados...")

    # 3. Construir Informe Agrupado por CATEGORÍA
    df_categoria = df_detalle.groupby('Categoría').agg(
        Total_Productos=('SKU', 'count'),
        Ventas_Totales_Periodo=('Ventas Totales del Periodo', 'sum'),
        Total_Ingresado_Lote=('Cantidad Total Recibida', 'sum'),
        Stock_Actual_Global=('Stock Actual', 'sum')
    ).reset_index()
    
    # Calcular % de éxito (Sell Through) a nivel de Categoría
    df_categoria['% Sell-Through (Éxito del Lote)'] = (
        df_categoria['Ventas_Totales_Periodo'] / 
        df_categoria['Total_Ingresado_Lote'].replace(0, 1) * 100
    ).round(2)
    df_categoria = df_categoria.sort_values('Ventas_Totales_Periodo', ascending=False)

    # 4. Construir Informe Agrupado por SUBCATEGORÍA
    df_subcategoria = df_detalle.groupby(['Categoría', 'Subcategoría']).agg(
        Total_Productos=('SKU', 'count'),
        Ventas_Totales_Periodo=('Ventas Totales del Periodo', 'sum'),
        Total_Ingresado_Lote=('Cantidad Total Recibida', 'sum'),
        Stock_Actual_Global=('Stock Actual', 'sum')
    ).reset_index()
    
    # Calcular % de éxito (Sell Through) a nivel de Subcategoría
    df_subcategoria['% Sell-Through (Éxito del Lote)'] = (
        df_subcategoria['Ventas_Totales_Periodo'] / 
        df_subcategoria['Total_Ingresado_Lote'].replace(0, 1) * 100
    ).round(2)
    df_subcategoria = df_subcategoria.sort_values('Ventas_Totales_Periodo', ascending=False)

    # 5. Exportar todo a Excel en 3 Pestañas Distintas
    print("💾 Guardando el informe en Excel...")
    with pd.ExcelWriter(ruta_excel, engine='openpyxl') as writer:
        df_detalle.to_excel(writer, sheet_name='Detalle por Producto', index=False)
        df_categoria.to_excel(writer, sheet_name='Resumen CATEGORIAS', index=False)
        df_subcategoria.to_excel(writer, sheet_name='Resumen SUBCATEGORIAS', index=False)

    print(f"🎉 ¡Reporte Final Generado Exitosamente!")
    print(f"📁 Búscalo aquí: {ruta_excel}")
    conn.close()

if __name__ == "__main__":
    generar_reporte()
