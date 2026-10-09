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
import plotly.graph_objects as go
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
# Maestra de equipos: asocia cada código de equipo con su descripción y su zona.
# El nombre lleva mayúsculas y PostgREST es sensible a ellas.
ZONAS_TABLA = _config("SUPABASE_ZONAS_TABLE", defecto="Maestro_Equipos_Zonas")

# Paleta de marca, validada para fondo claro.
#
# VERDE es el color institucional y pinta todas las barras de magnitud: al ser
# series únicas, cada gráfico se distingue por su título y no por el matiz, lo
# que evita depender del color para leer el dato. Su contraste contra el fondo es
# 2.21:1, por debajo de 3:1, así que las barras llevan siempre su valor escrito
# al lado y existe la vista de tabla en Detalle.
#
# VERDE_OSCURO es el único que admite texto blanco encima (5.1:1); sobre VERDE el
# texto va oscuro (8.7:1), nunca blanco.
#
# ROJO queda reservado para estado (alertas) y no se usa como color de serie.
VERDE = "#7DBE00"
VERDE_OSCURO = "#4e7a00"
VERDE_TENUE = "#eff7e0"
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
    "nombre_equipo": "Descripción equipo",
    "zona": "Zona",
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
    "registrado_en": "Fecha digitación",
}

FORMATO_FECHA = "%d/%m/%Y"  # fecha y fecha de digitación se muestran igual

# Columnas que se piden a la base. Se excluyen las internas (id_registro,
# fecha_original, telegram_chat_id, telegram_usuario): no se muestran nunca.
COLUMNAS_CONSULTA = ",".join([
    "ficha", "nombre_completo", "ip_equipo", "tiene_implemento",
    "cantidad_implementos", "implemento_1", "implemento_2", "fecha",
    "area_trabajada", "hora_inicio", "horometro_inicio", "hda", "ste", "labor",
    "hora_final", "horometro_final", "horas_trabajadas", "horometro_diferencia",
    "tiene_observaciones", "observaciones", "registrado_en",
])

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
    "zona",
    "labor",
    "actividad",
    "hda",
    "ste",
    "ficha",
    "nombre_completo",
    "ip_equipo",
    "nombre_equipo",
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


@st.cache_resource(show_spinner=False)
def _cliente_anonimo():
    """Cliente sin sesión, para las operaciones de Auth.

    `create_client` cuesta ~380 ms, así que se crea una sola vez por proceso.
    """
    from supabase import create_client

    return create_client(SUPABASE_URL, SUPABASE_KEY)


@st.cache_resource(show_spinner=False, max_entries=20)
def _cliente_sesion(usuario_id: str):
    """Un cliente por usuario, reutilizado entre reruns.

    El token NO forma parte de la clave porque rota cada hora; se adjunta en
    cada uso con `postgrest.auth()`, que sólo reescribe la cabecera.
    """
    from supabase import create_client

    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _cliente(usuario_id: str, token: str):
    cliente = _cliente_sesion(usuario_id)
    cliente.postgrest.auth(token)
    return cliente


def _descargar(tabla: str, usuario_id: str, token: str, columnas: str = "*") -> pd.DataFrame:
    cliente = _cliente(usuario_id, token)
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
def cargar_registros(tabla: str, usuario_id: str, _token: str) -> pd.DataFrame:
    """Registros del usuario. `_token` lleva guion bajo para quedar fuera de la
    clave de caché: así la rotación horaria del token no obliga a recargar."""
    return _descargar(tabla, usuario_id, _token, COLUMNAS_CONSULTA)


@st.cache_data(ttl=3600, show_spinner=False)
def cargar_actividades(
    tabla: str, codigos: tuple[str, ...], usuario_id: str, _token: str
) -> pd.DataFrame:
    """Sólo los nombres de los códigos presentes en los registros.

    La maestra tiene ~1600 filas (dos páginas, ~800 ms). Pidiendo los códigos
    que de verdad aparecen, una página basta (~190 ms).
    """
    if not codigos:
        return pd.DataFrame(columns=["codigo", "nome"])

    cliente = _cliente(usuario_id, _token)
    respuesta = (
        cliente.table(tabla).select("codigo,nome").in_("codigo", list(codigos)).execute()
    )
    maestro = pd.DataFrame(respuesta.data or [])
    if maestro.empty:
        return maestro
    maestro["codigo"] = maestro["codigo"].astype(str).str.strip()
    maestro["nome"] = maestro["nome"].astype(str).str.strip()
    return maestro.drop_duplicates(subset="codigo")


SIN_ZONA = "Sin zona"


@st.cache_data(ttl=3600, show_spinner=False)
def cargar_zonas(tabla: str, usuario_id: str, _token: str) -> pd.DataFrame:
    """Maestra de equipos: código, descripción y zona."""
    maestro = _descargar(tabla, usuario_id, _token, "Cod_equipo,D_equipo,Zona")
    if maestro.empty:
        return maestro
    maestro = maestro.rename(
        columns={"Cod_equipo": "codigo_equipo", "D_equipo": "nombre_equipo", "Zona": "zona"}
    )
    for columna in ("codigo_equipo", "nombre_equipo", "zona"):
        if columna in maestro.columns:
            # Las tres columnas de la maestra son nullable: un NULL llegaría como
            # el texto "None" y se mostraría tal cual en el filtro.
            maestro[columna] = (
                maestro[columna]
                .astype(str)
                .str.strip()
                .replace({"nan": "", "None": "", "<NA>": "", "null": ""})
            )
    # La llave de cruce se normaliza a mayúsculas: los códigos vienen de dos
    # capturas distintas y no siempre coinciden en caja.
    maestro["llave"] = maestro["codigo_equipo"].str.upper()
    return maestro.drop_duplicates(subset="llave")


def agregar_zonas(df: pd.DataFrame, usuario_id: str, token: str) -> pd.DataFrame:
    """Cruza el equipo del registro contra la maestra para traer zona y descripción."""
    df = df.copy()
    df["zona"] = SIN_ZONA
    df["nombre_equipo"] = df.get("ip_equipo", "")

    if "ip_equipo" not in df.columns:
        return df
    try:
        maestro = cargar_zonas(ZONAS_TABLA, usuario_id, token)
    except Exception as error:
        st.warning(f"No se pudo leer la maestra de equipos y zonas: {error}")
        return df
    if maestro.empty:
        st.warning(
            f"No se pudo leer «{ZONAS_TABLA}»: los registros quedan sin zona. "
            "Revise la política de lectura de esa tabla."
        )
        return df

    llaves = df["ip_equipo"].astype(str).str.strip().str.upper()
    zonas = dict(zip(maestro["llave"], maestro["zona"]))
    nombres = dict(zip(maestro["llave"], maestro["nombre_equipo"]))
    # Un equipo que no esté en la maestra conserva su registro y queda en
    # "Sin zona": nunca se pierde una fila por un faltante del maestro.
    df["zona"] = llaves.map(zonas).replace("", pd.NA).fillna(SIN_ZONA)
    df["nombre_equipo"] = llaves.map(nombres).replace("", pd.NA).fillna(df["ip_equipo"])
    return df


def agregar_actividades(df: pd.DataFrame, usuario_id: str, token: str) -> pd.DataFrame:
    """Traduce el código de `labor` al nombre de la actividad usando la tabla maestra."""
    df = df.copy()
    if "labor" not in df.columns:
        df["actividad"] = ""
        return df

    codigos = df["labor"].astype(str).str.strip()
    df["actividad"] = codigos  # si no hay maestra, al menos se ve el código

    presentes = tuple(sorted(c for c in codigos.unique() if c))
    try:
        maestro = cargar_actividades(ACTIVIDADES_TABLA, presentes, usuario_id, token)
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


def obtener_datos(usuario_id: str, token: str) -> pd.DataFrame:
    """Lee los registros como el usuario autenticado y los deja listos para graficar."""
    try:
        datos = cargar_registros(SUPABASE_TABLA, usuario_id, token)
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

    datos = agregar_actividades(normalizar(datos), usuario_id, token)
    return agregar_zonas(datos, usuario_id, token)


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
        "id": getattr(respuesta.user, "id", ""),
    }


def cerrar_sesion() -> None:
    try:
        _cliente_anonimo().auth.sign_out()
    except Exception:  # cerrar la sesión local es lo que importa
        pass
    st.session_state.pop("sesion", None)
    st.session_state.pop("datos_listos", None)
    st.cache_data.clear()


def iniciar_sesion(correo: str, contrasena: str) -> str | None:
    """Devuelve un mensaje de error, o None si la sesión quedó abierta."""
    try:
        respuesta = _cliente_anonimo().auth.sign_in_with_password(
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
            _guardar_sesion(_cliente_anonimo().auth.refresh_session(sesion["refresh_token"]))
        except Exception:
            st.session_state.pop("sesion", None)
            return None
    return st.session_state["sesion"]["access_token"]


def pantalla_ingreso() -> None:
    _, centro, _ = st.columns([1, 1.6, 1])
    with centro:
        st.write("")
        with st.container(border=True):
            encabezado_marca("Ingrese con el usuario que le asignaron.")
            with st.form("ingreso", border=False):
                correo = st.text_input("Correo", placeholder="nombre@empresa.com")
                contrasena = st.text_input("Contraseña", type="password", placeholder="••••••••")
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
                        unidad: str, color: str = VERDE, alto: int | None = None):
    datos = datos.sort_values(valor, ascending=True)
    fig = go.Figure(
        go.Bar(
            x=datos[valor],
            y=datos[categoria].astype(str),
            orientation="h",
            marker=dict(color=color, line_width=0, cornerradius=4),
            texttemplate="%{x:,.1f}",
            textposition="outside",
            textfont=dict(color=TINTA_SUAVE, size=12),
            cliponaxis=False,
            hovertemplate="<b>%{y}</b><br>%{x:,.2f} " + unidad + "<extra></extra>",
        )
    )
    fig.update_xaxes(showgrid=True, gridcolor=REJILLA, zeroline=False, title=None)
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
    fig = go.Figure(
        go.Bar(
            x=datos["fecha"],
            y=datos["horas_trabajadas"],
            marker=dict(color=VERDE, line_width=0, cornerradius=4),
            texttemplate="%{y:,.1f}",
            textposition="outside",
            textfont=dict(color=TINTA_SUAVE, size=12),
            cliponaxis=False,
            hovertemplate="<b>%{x|%d/%m/%Y}</b><br>%{y:,.2f} horas<extra></extra>",
        )
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
    for columna in ("fecha", "registrado_en"):
        if columna in tabla.columns:
            tabla[columna] = tabla[columna].dt.strftime(FORMATO_FECHA)
    return tabla.rename(columns=ETIQUETAS)


# --------------------------------------------------------------------------- #
# Interfaz
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Labores de maquinaria", page_icon="🚜", layout="wide")

# Hoja de estilos. Los selectores por data-testid son los puntos de enganche de
# Streamlit; si alguno cambia de nombre en una versión futura, la regla
# simplemente deja de aplicar y la app sigue funcionando sin romperse.
ESTILOS = f"""
<style>
  :root {{
    --verde: {VERDE};
    --verde-oscuro: {VERDE_OSCURO};
    --verde-tenue: {VERDE_TENUE};
    --tinta: {TINTA};
    --tinta-suave: {TINTA_SUAVE};
    --rejilla: {REJILLA};
  }}

  /* Títulos con un acento de marca a la izquierda */
  h1 {{
    font-weight: 700 !important;
    letter-spacing: -0.02em;
    padding-left: 14px;
    border-left: 5px solid var(--verde);
    line-height: 1.15;
  }}
  h2, h3 {{ font-weight: 650 !important; color: var(--tinta); }}

  /* Tarjetas de indicadores */
  div[data-testid="stMetric"] {{
    background: #ffffff;
    border: 1px solid var(--rejilla);
    border-top: 3px solid var(--verde);
    border-radius: 12px;
    padding: 14px 18px;
    box-shadow: 0 1px 2px rgba(11, 11, 11, 0.04);
  }}
  div[data-testid="stMetricValue"] {{
    font-size: 1.65rem;
    font-weight: 700;
    color: var(--tinta);
  }}
  div[data-testid="stMetricLabel"] {{
    color: var(--tinta-suave);
    font-size: 0.82rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}

  /* Pestañas: la activa queda marcada en verde */
  button[data-baseweb="tab"] {{ font-weight: 600; }}
  div[data-baseweb="tab-highlight"] {{ background-color: var(--verde) !important; }}

  /* Botones. Sobre el verde el texto va oscuro: en blanco no alcanza contraste */
  div.stButton > button, div.stFormSubmitButton > button {{
    background: var(--verde);
    color: var(--tinta) !important;
    border: 1px solid var(--verde-oscuro);
    border-radius: 9px;
    font-weight: 650;
    transition: filter 120ms ease;
  }}
  div.stButton > button:hover, div.stFormSubmitButton > button:hover {{
    filter: brightness(1.06);
    border-color: var(--verde-oscuro);
  }}
  div[data-testid="stDownloadButton"] > button {{
    background: #ffffff;
    color: var(--tinta) !important;
    border: 1px solid var(--rejilla);
    border-radius: 9px;
    font-weight: 600;
  }}
  div[data-testid="stDownloadButton"] > button:hover {{
    border-color: var(--verde-oscuro);
    background: var(--verde-tenue);
  }}

  /* Barra lateral */
  section[data-testid="stSidebar"] {{ border-right: 1px solid var(--rejilla); }}
  section[data-testid="stSidebar"] h2 {{
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--tinta-suave);
  }}

  /* Etiquetas de los filtros seleccionados */
  span[data-baseweb="tag"] {{
    background-color: var(--verde) !important;
    color: var(--tinta) !important;
  }}

  /* Pantalla de ingreso y estado de carga */
  .marca {{
    display: flex; align-items: center; gap: 12px; margin-bottom: 4px;
  }}
  .marca-punto {{
    width: 34px; height: 34px; border-radius: 10px;
    background: var(--verde); flex: 0 0 auto;
    display: grid; place-items: center; font-size: 1.1rem;
  }}
  .marca-texto {{
    font-size: 1.32rem; font-weight: 700; color: var(--tinta);
    letter-spacing: -0.01em; line-height: 1.2;
  }}
  .marca-sub {{ color: var(--tinta-suave); font-size: 0.9rem; margin: 2px 0 18px; }}

  .cargando {{
    border: 1px solid var(--rejilla);
    border-radius: 14px;
    background: #ffffff;
    padding: 34px 32px;
    max-width: 440px;
    margin: 56px auto;
    text-align: center;
    box-shadow: 0 2px 10px rgba(11, 11, 11, 0.05);
  }}
  .cargando-titulo {{
    font-size: 1.05rem; font-weight: 650; color: var(--tinta); margin-bottom: 6px;
  }}
  .cargando-nota {{ color: var(--tinta-suave); font-size: 0.86rem; }}
  .cargando-pista {{
    position: relative; height: 6px; border-radius: 99px;
    background: var(--verde-tenue); overflow: hidden; margin: 20px 0 14px;
  }}
  .cargando-pista::after {{
    content: ""; position: absolute; inset: 0;
    width: 40%; border-radius: 99px; background: var(--verde);
    animation: avance 1.1s ease-in-out infinite;
  }}
  @keyframes avance {{
    0%   {{ transform: translateX(-105%); }}
    100% {{ transform: translateX(255%); }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .cargando-pista::after {{ animation: none; width: 100%; opacity: 0.55; }}
  }}
</style>
"""
st.markdown(ESTILOS, unsafe_allow_html=True)

TARJETA_CARGANDO = """
<div class="cargando">
  <div class="cargando-titulo">Cargando registros</div>
  <div class="cargando-nota">Consultando la base de datos…</div>
  <div class="cargando-pista"></div>
  <div class="cargando-nota">Puede tardar unos segundos la primera vez del día.</div>
</div>
"""


def encabezado_marca(subtitulo: str) -> None:
    st.markdown(
        f"""
        <div class="marca">
          <div class="marca-punto">🚜</div>
          <div class="marca-texto">Labores de maquinaria</div>
        </div>
        <div class="marca-sub">{subtitulo}</div>
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

# La tarjeta se muestra solo mientras no haya datos en la sesión. En los reruns
# por cambio de filtro los datos vienen de la caché, y hacerla aparecer daba la
# impresión de que la app volvía a consultar la base.
marcador_carga = st.empty()
if not st.session_state.get("datos_listos"):
    marcador_carga.markdown(TARJETA_CARGANDO, unsafe_allow_html=True)
datos = obtener_datos(st.session_state["sesion"]["id"], token)
marcador_carga.empty()
if not datos.empty:
    st.session_state["datos_listos"] = True

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
    suertes = multiselector("ste", "Suerte")
    actividades = multiselector("actividad", "Actividad")
    equipos = multiselector("ip_equipo", "Equipo")
    solo_alertas = st.checkbox("Sólo registros con alerta")
    solo_observaciones = st.checkbox("Sólo registros con observación")
    st.caption("Los filtros de Zona y Hacienda están en la pestaña Detalle y aplican a todo el tablero.")

# Opciones tomadas del conjunto completo, para que no cambien al filtrar.
def _opciones(columna: str) -> list[str]:
    if columna not in datos.columns:
        return []
    return sorted(v for v in datos[columna].dropna().unique() if str(v).strip())


ZONAS_DISPONIBLES = _opciones("zona")
HACIENDAS_DISPONIBLES = _opciones("hda")


# Los selectores se dibujan en la pestaña Detalle; Streamlit deja su valor en
# session_state antes de ejecutar el script, así que aquí ya están disponibles.
def _seleccion(clave: str, validas: list[str]) -> list[str]:
    return [v for v in (st.session_state.get(clave) or []) if v in validas]


zonas = _seleccion("filtro_zona", ZONAS_DISPONIBLES)
haciendas = _seleccion("filtro_hacienda", HACIENDAS_DISPONIBLES)

filtrados = datos.copy()
if rango and isinstance(rango, (list, tuple)) and len(rango) == 2:
    desde, hasta = pd.Timestamp(rango[0]), pd.Timestamp(rango[1])
    filtrados = filtrados[filtrados["fecha"].between(desde, hasta) | filtrados["fecha"].isna()]
for columna, seleccion in [
    ("zona", zonas),
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
        datos["registrado_en"].max().strftime(FORMATO_FECHA + " %H:%M")
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
columnas_kpi[4].metric("Zonas", filtrados["zona"].nunique())
columnas_kpi[5].metric("Con alerta", int(filtrados["tiene_alerta"].sum()))

detalle, resumen, calidad = st.tabs(["Detalle", "Resumen", "Calidad de datos"])

with resumen:
    por_zona = filtrados.groupby("zona", as_index=False)["horas_trabajadas"].sum()
    st.plotly_chart(
        barras_horizontales(por_zona, "zona", "horas_trabajadas",
                            "Horas trabajadas por zona", "horas"),
        width="stretch",
    )

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
                            "Horas trabajadas por hacienda", "horas"),
        width="stretch",
    )

    por_dia = filtrados.dropna(subset=["fecha"]).groupby("fecha", as_index=False)["horas_trabajadas"].sum()
    st.plotly_chart(barras_por_dia(por_dia, "Horas trabajadas por día"), width="stretch")

    izquierda, derecha = st.columns(2)
    por_actividad = filtrados.groupby("actividad", as_index=False)["area_trabajada"].sum()
    izquierda.plotly_chart(
        barras_horizontales(por_actividad, "actividad", "area_trabajada",
                            "Área trabajada por actividad", "ha"),
        width="stretch",
    )

    por_equipo = (
        filtrados.groupby("nombre_equipo", as_index=False)["horas_trabajadas"]
        .sum()
        .nlargest(15, "horas_trabajadas")
    )
    derecha.plotly_chart(
        barras_horizontales(por_equipo, "nombre_equipo", "horas_trabajadas",
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
    col_zona, col_hacienda = st.columns(2)
    col_zona.multiselect(
        "Zona",
        ZONAS_DISPONIBLES,
        key="filtro_zona",
        placeholder="Todas las zonas",
        help="Filtra los equipos por zona. Aplica también a Resumen y Calidad de datos.",
    )
    col_hacienda.multiselect(
        "Hacienda",
        HACIENDAS_DISPONIBLES,
        key="filtro_hacienda",
        placeholder="Todas las haciendas",
        help="Aplica también a Resumen y Calidad de datos.",
    )

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
