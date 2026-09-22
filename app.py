"""Dashboard del Registro Diario de Labores de Maquinaria.

Fuente de datos: la tabla de Supabase que alimenta el bot de Telegram (n8n).
El acceso es con usuario y contraseña de Supabase Auth: cada consulta viaja con
el token de quien inició sesión, así que las políticas RLS se aplican por usuario.

Ejecutar:  streamlit run app.py
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

def _config(*nombres: str, defecto: str = "") -> str:
    """Toma el valor de los Secrets de Streamlit Cloud y, si no está, del .env local."""
    for nombre in nombres:
        try:
            valor = st.secrets[nombre]
        except Exception:  # sin archivo de secrets (ejecución local)
            valor = None
        if not valor:
            valor = os.getenv(nombre)
        if valor:
            return str(valor).strip()
    return defecto


SUPABASE_URL = _config("SUPABASE_URL")
SUPABASE_KEY = _config("SUPABASE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY")
SUPABASE_TABLA = _config("SUPABASE_TABLE", defecto="registro_labores_maquinaria")
# Maestra de labores: el campo `labor` de cada registro es el código de la actividad.
ACTIVIDADES_TABLA = _config("SUPABASE_ACTIVIDADES_TABLE", defecto="actividades")

# Paleta validada para fondo claro: slot 1 azul, slot 2 naranja, slot 3 aqua.
AZUL = "#2a78d6"
NARANJA = "#eb6834"
AQUA = "#1baf7a"
ROJO = "#e34948"
TINTA = "#0b0b0b"
TINTA_SUAVE = "#52514e"
SUPERFICIE = "#fcfcfb"
REJILLA = "#eceae5"

ETIQUETAS = {
    "fecha": "Fecha",
    "ficha": "Ficha",
    "nombre_completo": "Operador",
    "ip_equipo": "Equipo",
    "tiene_implemento": "Con implemento",
    "cantidad_implementos": "N° implementos",
    "implemento_1": "Implemento 1",
    "implemento_2": "Implemento 2",
    "area_trabajada": "Área (ha)",
    "hora_inicio": "Hora inicio",
    "horometro_inicio": "Horómetro inicio",
    "hda": "Hacienda",
    "ste": "Suerte",
    "labor": "Código actividad",
    "actividad": "Actividad",
    "hora_final": "Hora final",
    "horometro_final": "Horómetro final",
    "horas_trabajadas": "Horas trabajadas",
    "horometro_diferencia": "Δ Horómetro",
    "tiene_observaciones": "Con observación",
    "observaciones": "Observaciones",
    "registrado_en": "Registrado en",
}

COLUMNAS_NUMERICAS = [
    "cantidad_implementos",
    "area_trabajada",
    "horometro_inicio",
    "horometro_final",
    "horas_trabajadas",
    "horometro_diferencia",
]
COLUMNAS_BOOLEANAS = ["tiene_implemento", "tiene_observaciones"]
COLUMNAS_TEXTO = [
    "ficha",
    "nombre_completo",
    "ip_equipo",
    "hda",
    "ste",
    "labor",
    "implemento_1",
    "implemento_2",
]

# Orden de columnas en las tablas de detalle.
ORDEN_COLUMNAS = [
    "fecha",
    "labor",
    "actividad",
    "hda",
    "ste",
    "ficha",
    "nombre_completo",
    "ip_equipo",
    "implemento_1",
    "implemento_2",
    "hora_inicio",
    "hora_final",
    "horas_trabajadas",
    "area_trabajada",
    "horometro_inicio",
    "horometro_final",
    "horometro_diferencia",
    "observaciones",
]


# --------------------------------------------------------------------------- #
# Carga y normalización
# --------------------------------------------------------------------------- #
def _a_booleano(serie: pd.Series) -> pd.Series:
    return serie.astype(str).str.strip().str.lower().isin(["true", "1", "t", "si", "sí", "yes", "y"])


def _a_fecha(serie: pd.Series) -> pd.Series:
    texto = serie.astype(str).str.strip()
    fechas = pd.to_datetime(texto, format="%d/%m/%Y", errors="coerce")
    pendientes = fechas.isna() & ~texto.isin(["", "nan", "None", "NaT"])
    if pendientes.any():
        fechas.loc[pendientes] = pd.to_datetime(
            texto[pendientes], errors="coerce", dayfirst=True, format="mixed"
        )
    return fechas


def _a_horas(serie: pd.Series) -> pd.Series:
    """Convierte 06:30 en 6.5 para contrastar el horario contra las horas reportadas."""
    partes = serie.astype(str).str.strip().str.extract(r"^(\d{1,2}):(\d{2})")
    horas = pd.to_numeric(partes[0], errors="coerce")
    minutos = pd.to_numeric(partes[1], errors="coerce")
    return horas + minutos / 60


def normalizar(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    for columna in COLUMNAS_NUMERICAS:
        if columna in df.columns:
            df[columna] = pd.to_numeric(
                df[columna].astype(str).str.strip().str.replace(",", ".", regex=False),
                errors="coerce",
            )
    for columna in COLUMNAS_BOOLEANAS:
        if columna in df.columns:
            df[columna] = _a_booleano(df[columna])
    for columna in COLUMNAS_TEXTO + ["observaciones"]:
        if columna in df.columns:
            df[columna] = (
                df[columna].astype(str).str.strip().replace({"nan": "", "None": "", "<NA>": ""})
            )

    if "fecha" in df.columns:
        df["fecha"] = _a_fecha(df["fecha"])
    else:
        df["fecha"] = pd.NaT

    if "registrado_en" in df.columns:
        # El bot guarda en UTC; se muestra en hora de Colombia (UTC-5).
        df["registrado_en"] = pd.to_datetime(
            df["registrado_en"], errors="coerce", utc=True
        ).dt.tz_localize(None) - pd.Timedelta(hours=5)

    inicio = _a_horas(df["hora_inicio"]) if "hora_inicio" in df.columns else pd.Series(index=df.index, dtype=float)
    final = _a_horas(df["hora_final"]) if "hora_final" in df.columns else pd.Series(index=df.index, dtype=float)
    jornada = final - inicio
    df["horas_calendario"] = jornada.where(jornada >= 0, jornada + 24)

    df["clave"] = (
        df.get("ficha", pd.Series("", index=df.index)).astype(str)
        + "|"
        + df["fecha"].dt.strftime("%Y-%m-%d").fillna("")
        + "|"
        + df.get("hora_inicio", pd.Series("", index=df.index)).astype(str)
        + "|"
        + df.get("labor", pd.Series("", index=df.index)).astype(str)
    )

    orden = [c for c in ["fecha", "nombre_completo", "hora_inicio"] if c in df.columns]
    if orden:
        df = df.sort_values(orden, na_position="last")
    return df.reset_index(drop=True)


def _cliente(token: str | None = None):
    """Cliente de Supabase. Con token, las consultas viajan como usuario autenticado."""
    from supabase import create_client

    cliente = create_client(SUPABASE_URL, SUPABASE_KEY)
    if token:
        cliente.postgrest.auth(token)
    return cliente


def _descargar(tabla: str, token: str, columnas: str = "*") -> pd.DataFrame:
    cliente = _cliente(token)
    filas: list[dict] = []
    tamano, pagina = 1000, 0
    while True:
        respuesta = (
            cliente.table(tabla)
            .select(columnas)
            .range(pagina * tamano, pagina * tamano + tamano - 1)
            .execute()
        )
        lote = respuesta.data or []
        filas.extend(lote)
        if len(lote) < tamano:
            break
        pagina += 1
    return pd.DataFrame(filas)


@st.cache_data(ttl=300, show_spinner=False)
def cargar_registros(tabla: str, token: str) -> pd.DataFrame:
    return _descargar(tabla, token)


@st.cache_data(ttl=1800, show_spinner=False)
def cargar_actividades(tabla: str, token: str) -> pd.DataFrame:
    maestro = _descargar(tabla, token, "codigo,nome")
    if maestro.empty:
        return maestro
    maestro["codigo"] = maestro["codigo"].astype(str).str.strip()
    maestro["nome"] = maestro["nome"].astype(str).str.strip()
    return maestro.drop_duplicates(subset="codigo")


def agregar_actividades(df: pd.DataFrame, token: str) -> pd.DataFrame:
    """Traduce el código de `labor` al nombre de la actividad usando la tabla maestra."""
    df = df.copy()
    if "labor" not in df.columns:
        df["actividad"] = ""
        return df

    codigos = df["labor"].astype(str).str.strip()
    df["actividad"] = codigos  # si no hay maestra, al menos se ve el código

    try:
        maestro = cargar_actividades(ACTIVIDADES_TABLA, token)
    except Exception as error:  # la app sigue funcionando con el código a la vista
        st.warning(f"No se pudo leer la tabla de actividades: {error}")
        return df
    if maestro.empty:
        st.warning(
            f"No se pudo leer la maestra «{ACTIVIDADES_TABLA}»: se muestran los códigos "
            "de actividad en lugar de los nombres. Revise la política de lectura de esa tabla."
        )
        return df

    nombres = codigos.map(dict(zip(maestro["codigo"], maestro["nome"])))
    df["actividad"] = nombres.fillna(codigos).replace("", pd.NA).fillna(codigos)
    return df


def obtener_datos(token: str) -> pd.DataFrame:
    """Lee los registros como el usuario autenticado y los deja listos para graficar."""
    try:
        datos = cargar_registros(SUPABASE_TABLA, token)
    except Exception as error:
        st.error(f"No se pudo consultar la base de datos: {error}")
        return pd.DataFrame()

    if datos.empty:
        st.warning(
            f"La consulta a «{SUPABASE_TABLA}» no devolvió registros. Si la tabla tiene "
            "datos, revise que exista la política de lectura (SELECT) para el rol "
            "`authenticated`."
        )
        return pd.DataFrame()

    return agregar_actividades(normalizar(datos), token)


# --------------------------------------------------------------------------- #
# Autenticación con Supabase Auth
# --------------------------------------------------------------------------- #
MARGEN_REFRESCO = 120  # segundos antes del vencimiento en que se renueva el token


def _guardar_sesion(respuesta) -> None:
    sesion = respuesta.session
    st.session_state["sesion"] = {
        "access_token": sesion.access_token,
        "refresh_token": sesion.refresh_token,
        "expira_en": float(sesion.expires_at or 0),
        "correo": getattr(respuesta.user, "email", ""),
    }


def cerrar_sesion() -> None:
    try:
        _cliente().auth.sign_out()
    except Exception:  # cerrar la sesión local es lo que importa
        pass
    st.session_state.pop("sesion", None)
    st.cache_data.clear()


def iniciar_sesion(correo: str, contrasena: str) -> str | None:
    """Devuelve un mensaje de error, o None si la sesión quedó abierta."""
    try:
        respuesta = _cliente().auth.sign_in_with_password(
            {"email": correo.strip().lower(), "password": contrasena}
        )
    except Exception as error:
        detalle = str(error).lower()
        if "invalid login" in detalle or "credentials" in detalle:
            return "Correo o contraseña incorrectos."
        if "not confirmed" in detalle:
            return "El usuario aún no está confirmado. Pida que lo activen en Supabase."
        if "rate limit" in detalle or "too many" in detalle:
            return "Demasiados intentos seguidos. Espere un momento y vuelva a intentar."
        return f"No se pudo iniciar sesión: {error}"

    if not respuesta.session:
        return "El servidor no devolvió una sesión válida."
    _guardar_sesion(respuesta)
    return None


def token_activo() -> str | None:
    """Token vigente del usuario, renovado si está por vencer."""
    sesion = st.session_state.get("sesion")
    if not sesion:
        return None
    if sesion["expira_en"] and time.time() > sesion["expira_en"] - MARGEN_REFRESCO:
        try:
            _guardar_sesion(_cliente().auth.refresh_session(sesion["refresh_token"]))
        except Exception:
            st.session_state.pop("sesion", None)
            return None
    return st.session_state["sesion"]["access_token"]


def pantalla_ingreso() -> None:
    _, centro, _ = st.columns([1, 2, 1])
    with centro:
        st.title("Labores de maquinaria")
        st.caption("Ingrese con el usuario que le asignaron para consultar los registros.")
        with st.form("ingreso"):
            correo = st.text_input("Correo")
            contrasena = st.text_input("Contraseña", type="password")
            enviar = st.form_submit_button("Ingresar", width="stretch")
        if enviar:
            if not correo or not contrasena:
                st.error("Escriba el correo y la contraseña.")
                return
            error = iniciar_sesion(correo, contrasena)
            if error:
                st.error(error)
            else:
                st.rerun()


# --------------------------------------------------------------------------- #
# Validaciones previas a la digitación en BIOSA
# --------------------------------------------------------------------------- #
REGLAS = {
    "Horómetro sin avance": "El horómetro no cambió aunque se reportaron horas trabajadas.",
    "Horómetro final menor al inicial": "El horómetro final quedó por debajo del inicial.",
    "Avance de horómetro mayor a las horas": "El horómetro avanzó más de lo que dura la jornada reportada.",
    "Horas no coinciden con el horario": "Las horas trabajadas no cuadran con hora inicio / hora final.",
    "Sin área registrada": "El registro quedó sin área trabajada.",
}


def marcar_alertas(df: pd.DataFrame) -> pd.DataFrame:
    horas = df["horas_trabajadas"].fillna(0)
    delta = df["horometro_diferencia"].fillna(0)
    alertas = pd.DataFrame(index=df.index)
    alertas["Horómetro sin avance"] = (delta <= 0) & (horas > 0)
    alertas["Horómetro final menor al inicial"] = df["horometro_final"] < df["horometro_inicio"]
    alertas["Avance de horómetro mayor a las horas"] = delta > (horas + 1)
    alertas["Horas no coinciden con el horario"] = (horas - df["horas_calendario"]).abs() > 0.5
    alertas["Sin área registrada"] = df["area_trabajada"].fillna(0) <= 0
    return alertas.fillna(False).astype(bool)


# --------------------------------------------------------------------------- #
# Gráficos
# --------------------------------------------------------------------------- #
def _estilo(fig, titulo: str, alto: int) -> None:
    fig.update_layout(
        title=dict(text=titulo, font=dict(size=16, color=TINTA), x=0, xanchor="left"),
        plot_bgcolor=SUPERFICIE,
        paper_bgcolor=SUPERFICIE,
        font=dict(size=13, color=TINTA_SUAVE),
        margin=dict(l=8, r=36, t=52, b=8),
        height=alto,
        bargap=0.38,
        showlegend=False,
        hoverlabel=dict(bgcolor="#ffffff", font_size=13, bordercolor=REJILLA),
    )


def barras_horizontales(datos: pd.DataFrame, categoria: str, valor: str, titulo: str,
                        unidad: str, color: str = AZUL, alto: int | None = None):
    datos = datos.sort_values(valor, ascending=True)
    fig = px.bar(datos, x=valor, y=categoria, orientation="h")
    fig.update_traces(
        marker_color=color,
        marker_line_width=0,
        marker_cornerradius=4,
        texttemplate="%{x:,.1f}",
        textposition="outside",
        textfont=dict(color=TINTA_SUAVE, size=12),
        cliponaxis=False,
        hovertemplate="<b>%{y}</b><br>%{x:,.2f} " + unidad + "<extra></extra>",
    )
    fig.update_xaxes(showgrid=True, gridcolor=REJILLA, zeroline=False, title=None, ticksuffix="")
    # type="category" evita que Plotly lea como número los códigos de hacienda o
    # de actividad; automargin evita que se corten las etiquetas largas.
    fig.update_yaxes(
        showgrid=False,
        title=None,
        ticklabelposition="outside",
        automargin=True,
        type="category",
    )
    _estilo(fig, titulo, alto or max(220, 34 * len(datos) + 90))
    return fig


def barras_por_dia(datos: pd.DataFrame, titulo: str):
    fig = px.bar(datos, x="fecha", y="horas_trabajadas")
    fig.update_traces(
        marker_color=AZUL,
        marker_line_width=0,
        marker_cornerradius=4,
        texttemplate="%{y:,.1f}",
        textposition="outside",
        textfont=dict(color=TINTA_SUAVE, size=12),
        cliponaxis=False,
        hovertemplate="<b>%{x|%d/%m/%Y}</b><br>%{y:,.2f} horas<extra></extra>",
    )
    fig.update_xaxes(showgrid=False, title=None, tickformat="%d/%m", dtick="D1")
    fig.update_yaxes(showgrid=True, gridcolor=REJILLA, zeroline=False, title=None)
    _estilo(fig, titulo, 320)
    return fig


# --------------------------------------------------------------------------- #
# Exportación
# --------------------------------------------------------------------------- #
def a_excel(hojas: dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as escritor:
        for nombre, tabla in hojas.items():
            tabla.to_excel(escritor, sheet_name=nombre[:31], index=False)
    return buffer.getvalue()


def a_csv(tabla: pd.DataFrame) -> bytes:
    return tabla.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")


def para_mostrar(df: pd.DataFrame, columnas: list[str] | None = None) -> pd.DataFrame:
    columnas = [c for c in (columnas or df.columns) if c in df.columns]
    tabla = df[columnas].copy()
    if "fecha" in tabla.columns:
        tabla["fecha"] = tabla["fecha"].dt.strftime("%d/%m/%Y")
    if "registrado_en" in tabla.columns:
        tabla["registrado_en"] = tabla["registrado_en"].dt.strftime("%d/%m/%Y %H:%M")
    return tabla.rename(columns=ETIQUETAS)


# --------------------------------------------------------------------------- #
# Interfaz
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Labores de maquinaria", page_icon="🚜", layout="wide")

st.markdown(
    """
    <style>
      div[data-testid="stMetricValue"] { font-size: 1.7rem; }
      div[data-testid="stMetric"] {
        background: #ffffff; border: 1px solid #eceae5;
        border-radius: 10px; padding: 12px 16px;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

if not (SUPABASE_URL and SUPABASE_KEY):
    st.error(
        "Faltan las credenciales de conexión. Configúrelas en el archivo .env "
        "(local) o en los Secrets de Streamlit Cloud."
    )
    st.stop()

token = token_activo()
if not token:
    pantalla_ingreso()
    st.stop()

with st.sidebar:
    st.header("Sesión")
    st.caption(st.session_state["sesion"]["correo"])
    if st.button("Actualizar datos", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    if st.button("Cerrar sesión", width="stretch"):
        cerrar_sesion()
        st.rerun()

datos = obtener_datos(token)

if datos.empty:
    st.title("Registro diario de labores de maquinaria")
    st.stop()

alertas = marcar_alertas(datos)
datos["tiene_alerta"] = alertas.any(axis=1)
datos["detalle_alertas"] = alertas.apply(lambda fila: " · ".join(alertas.columns[fila]), axis=1)

with st.sidebar:
    st.header("Filtros")

    fechas_validas = datos["fecha"].dropna()
    if fechas_validas.empty:
        rango = None
    else:
        minimo, maximo = fechas_validas.min().date(), fechas_validas.max().date()
        rango = st.date_input(
            "Rango de fechas",
            value=(minimo, maximo),
            min_value=minimo,
            max_value=maximo,
            format="DD/MM/YYYY",
        )

    def multiselector(columna: str, etiqueta: str) -> list[str]:
        if columna not in datos.columns:
            return []
        opciones = sorted(v for v in datos[columna].dropna().unique() if str(v).strip())
        return st.multiselect(etiqueta, opciones, placeholder="Todos")

    operadores = multiselector("nombre_completo", "Operador")
    haciendas = multiselector("hda", "Hacienda")
    suertes = multiselector("ste", "Suerte")
    actividades = multiselector("actividad", "Actividad")
    equipos = multiselector("ip_equipo", "Equipo")
    solo_alertas = st.checkbox("Sólo registros con alerta")
    solo_observaciones = st.checkbox("Sólo registros con observación")

filtrados = datos.copy()
if rango and isinstance(rango, (list, tuple)) and len(rango) == 2:
    desde, hasta = pd.Timestamp(rango[0]), pd.Timestamp(rango[1])
    filtrados = filtrados[filtrados["fecha"].between(desde, hasta) | filtrados["fecha"].isna()]
for columna, seleccion in [
    ("nombre_completo", operadores),
    ("hda", haciendas),
    ("ste", suertes),
    ("actividad", actividades),
    ("ip_equipo", equipos),
]:
    if seleccion:
        filtrados = filtrados[filtrados[columna].isin(seleccion)]
if solo_alertas:
    filtrados = filtrados[filtrados["tiene_alerta"]]
if solo_observaciones and "tiene_observaciones" in filtrados.columns:
    filtrados = filtrados[filtrados["tiene_observaciones"]]

st.title("Registro diario de labores de maquinaria")
st.caption(
    f"{len(filtrados)} de {len(datos)} registros · último registro recibido: "
    + (
        datos["registrado_en"].max().strftime("%d/%m/%Y %H:%M")
        if "registrado_en" in datos.columns and datos["registrado_en"].notna().any()
        else "sin dato"
    )
)

if filtrados.empty:
    st.warning("Ningún registro cumple los filtros seleccionados.")
    st.stop()

columnas_kpi = st.columns(6)
columnas_kpi[0].metric("Registros", f"{len(filtrados):,}".replace(",", "."))
columnas_kpi[1].metric("Horas trabajadas", f"{filtrados['horas_trabajadas'].sum():,.1f}")
columnas_kpi[2].metric("Área trabajada (ha)", f"{filtrados['area_trabajada'].sum():,.2f}")
columnas_kpi[3].metric("Operadores", filtrados["nombre_completo"].nunique())
columnas_kpi[4].metric("Equipos", filtrados["ip_equipo"].nunique())
columnas_kpi[5].metric("Con alerta", int(filtrados["tiene_alerta"].sum()))

resumen, detalle, calidad = st.tabs(["Resumen", "Detalle", "Calidad de datos"])

with resumen:
    izquierda, derecha = st.columns(2)

    por_operador = (
        filtrados.groupby("nombre_completo", as_index=False)["horas_trabajadas"].sum().head(20)
    )
    izquierda.plotly_chart(
        barras_horizontales(por_operador, "nombre_completo", "horas_trabajadas",
                            "Horas trabajadas por operador", "horas"),
        width="stretch",
    )

    por_hacienda = filtrados.groupby("hda", as_index=False)["horas_trabajadas"].sum()
    derecha.plotly_chart(
        barras_horizontales(por_hacienda, "hda", "horas_trabajadas",
                            "Horas trabajadas por hacienda", "horas", color=NARANJA),
        width="stretch",
    )

    por_dia = filtrados.dropna(subset=["fecha"]).groupby("fecha", as_index=False)["horas_trabajadas"].sum()
    st.plotly_chart(barras_por_dia(por_dia, "Horas trabajadas por día"), width="stretch")

    izquierda, derecha = st.columns(2)
    por_actividad = filtrados.groupby("actividad", as_index=False)["area_trabajada"].sum()
    izquierda.plotly_chart(
        barras_horizontales(por_actividad, "actividad", "area_trabajada",
                            "Área trabajada por actividad", "ha", color=AQUA),
        width="stretch",
    )

    por_equipo = filtrados.groupby("ip_equipo", as_index=False)["horas_trabajadas"].sum().head(20)
    derecha.plotly_chart(
        barras_horizontales(por_equipo, "ip_equipo", "horas_trabajadas",
                            "Horas trabajadas por equipo", "horas"),
        width="stretch",
    )

    st.subheader("Consolidado por operador")
    tabla_operador = (
        filtrados.groupby(["ficha", "nombre_completo"], as_index=False)
        .agg(
            registros=("clave", "count"),
            horas=("horas_trabajadas", "sum"),
            area=("area_trabajada", "sum"),
            avance_horometro=("horometro_diferencia", "sum"),
            alertas=("tiene_alerta", "sum"),
        )
        .sort_values("horas", ascending=False)
        .rename(
            columns={
                "ficha": "Ficha",
                "nombre_completo": "Operador",
                "registros": "Registros",
                "horas": "Horas",
                "area": "Área (ha)",
                "avance_horometro": "Δ Horómetro",
                "alertas": "Alertas",
            }
        )
    )
    st.dataframe(tabla_operador, hide_index=True, width="stretch")

with detalle:
    st.subheader("Registros")
    columnas_detalle = [c for c in ORDEN_COLUMNAS if c in filtrados.columns] + ["registrado_en"]
    vista = para_mostrar(filtrados, columnas_detalle)
    st.dataframe(vista, hide_index=True, width="stretch", height=520)

    descarga_1, descarga_2 = st.columns(2)
    descarga_1.download_button(
        "Descargar Excel",
        a_excel({"Registros": vista}),
        file_name="labores_maquinaria.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )
    descarga_2.download_button(
        "Descargar CSV",
        a_csv(vista),
        file_name="labores_maquinaria.csv",
        mime="text/csv",
        width="stretch",
    )

with calidad:
    st.subheader("Alertas encontradas")
    conteo = (
        alertas.loc[filtrados.index]
        .sum()
        .reset_index()
        .set_axis(["regla", "registros"], axis=1)
    )
    conteo = conteo[conteo["registros"] > 0]

    if conteo.empty:
        st.success("Ningún registro filtrado presenta inconsistencias.")
    else:
        st.plotly_chart(
            barras_horizontales(conteo, "regla", "registros", "Registros por tipo de alerta",
                                "registros", color=ROJO),
            width="stretch",
        )
        for regla, descripcion in REGLAS.items():
            afectados = filtrados[alertas.loc[filtrados.index, regla]]
            if afectados.empty:
                continue
            with st.expander(f"{regla} — {len(afectados)} registro(s)"):
                st.caption(descripcion)
                st.dataframe(
                    para_mostrar(afectados, [c for c in ORDEN_COLUMNAS if c in afectados.columns]),
                    hide_index=True,
                    width="stretch",
                )

    st.subheader("Observaciones de los operadores")
    con_observacion = filtrados[filtrados["observaciones"].astype(str).str.strip() != ""]
    if con_observacion.empty:
        st.info("No hay observaciones en los registros filtrados.")
    else:
        st.dataframe(
            para_mostrar(
                con_observacion,
                ["fecha", "nombre_completo", "ip_equipo", "hda", "ste", "actividad", "observaciones"],
            ),
            hide_index=True,
            width="stretch",
        )
