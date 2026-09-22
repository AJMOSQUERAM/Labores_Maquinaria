# Registro diario de labores de maquinaria

Tablero en Streamlit que consulta los registros de labores de maquinaria
capturados por un bot de Telegram. Es una aplicación de **sólo lectura**: no
escribe nada en la base de datos.

Todo el tablero vive en un único archivo, [app.py](app.py).

---

## Arquitectura

```
Operador (Telegram)
      │
      ▼
   Flujo n8n ──────────► Supabase / Postgres
                          ├── registro_labores_maquinaria   (registros del bot)
                          └── actividades                   (maestra de labores)
                                    │
                                    ▼
                          app.py (Streamlit)
```

Dos identidades distintas tocan la base de datos:

| Quién | Clave | Permisos |
|---|---|---|
| Flujo de n8n | `service_role` | Escribe. Omite RLS por diseño. |
| Tablero | `anon` + sesión de usuario | Sólo `SELECT`, y únicamente tras iniciar sesión. |

El tablero nunca usa la `service_role`. Se conecta con la clave `anon`, que por
sí sola no ve ningún dato, y a partir del login adjunta el token del usuario a
cada consulta.

---

## Ciclo de ejecución

Streamlit re-ejecuta `app.py` completo en cada interacción. El orden importa:

1. **Configuración** — `_config()` resuelve las variables de entorno.
2. **Autenticación** — `token_activo()`. Sin token válido se dibuja
   `pantalla_ingreso()` y `st.stop()` corta ahí.
3. **Carga** — `obtener_datos(token)` → `cargar_registros()` (cacheada).
4. **Normalización** — `normalizar()` convierte tipos y deriva columnas.
5. **Enriquecimiento** — `agregar_actividades()` traduce el código de labor.
6. **Validación** — `marcar_alertas()` produce la matriz booleana de reglas.
7. **Filtros** — la barra lateral recorta el DataFrame a `filtrados`.
8. **Render** — tres pestañas: Resumen, Detalle, Calidad de datos.

Todo lo que se dibuja parte de `filtrados`; `datos` conserva el conjunto
completo para los contadores del encabezado.

---

## Configuración

`_config(*nombres, defecto)` busca cada nombre primero en `st.secrets` (nube) y
luego en las variables de entorno cargadas del `.env` (local). El mismo código
corre en los dos lados sin ramas condicionales.

| Variable | Obligatoria | Por defecto |
|---|---|---|
| `SUPABASE_URL` | sí | — |
| `SUPABASE_KEY` | sí | también acepta `SUPABASE_SERVICE_ROLE_KEY` o `SUPABASE_ANON_KEY` |
| `SUPABASE_TABLE` | no | `registro_labores_maquinaria` |
| `SUPABASE_ACTIVIDADES_TABLE` | no | `actividades` |

Si faltan `SUPABASE_URL` o `SUPABASE_KEY`, la app se detiene con un mensaje antes
de dibujar nada.

---

## Autenticación

Implementada sobre Supabase Auth (correo y contraseña).

- `iniciar_sesion()` llama a `sign_in_with_password` y traduce los errores
  crudos a mensajes en español. Guarda la sesión en `st.session_state["sesion"]`
  (`access_token`, `refresh_token`, `expira_en`, `correo`).
- `token_activo()` se ejecuta en cada rerun. Si al token le quedan menos de
  `MARGEN_REFRESCO` segundos (120), lo renueva con `refresh_session`. Si la
  renovación falla, descarta la sesión y devuelve `None`, con lo que el usuario
  vuelve al formulario.
- `_cliente(token)` crea el cliente y, cuando hay token, llama a
  `cliente.postgrest.auth(token)`. Desde ese punto las consultas llegan a
  Postgres como rol `authenticated` y RLS decide qué filas devuelve.
- `cerrar_sesion()` cierra la sesión remota, borra `session_state` y limpia la
  caché, para que los datos de un usuario no queden en pantalla del siguiente.

Consecuencia de diseño: **el control de acceso está en la base de datos, no en
la app**. Si las políticas RLS no conceden `SELECT` al rol `authenticated`, el
login funciona pero no llega ninguna fila. Ese caso se detecta explícitamente en
`obtener_datos()` y se reporta como problema de políticas, no como "sin datos".

Las políticas RLS y las cuentas de usuario se administran directamente en
Supabase; no forman parte de este repositorio.

---

## Caché

| Función | TTL | Clave de caché |
|---|---|---|
| `cargar_registros` | 300 s | `(tabla, token)` |
| `cargar_actividades` | 1800 s | `(tabla, token)` |

El token forma parte de la clave a propósito: evita que un usuario reciba datos
traídos con las credenciales de otro. Como el token rota en cada renovación, la
caché se invalida al menos una vez por hora, lo cual es aceptable con estos
volúmenes.

`_descargar()` pagina de 1000 en 1000 hasta agotar la tabla, así que no depende
del límite por defecto de PostgREST.

El botón **Actualizar datos** llama a `st.cache_data.clear()`.

---

## Modelo de datos

### Columnas que llegan de la base

| Columna | Tipo tras normalizar | Nota |
|---|---|---|
| `ficha`, `nombre_completo` | texto | Identifican al operador. |
| `ip_equipo`, `implemento_1`, `implemento_2` | texto | Códigos de equipo. |
| `tiene_implemento`, `tiene_observaciones` | booleano | Llegan como `"true"`/`"false"`. |
| `cantidad_implementos` | numérico | |
| `fecha` | datetime | Se parsea `%d/%m/%Y` y, si falla, en modo mixto con `dayfirst`. |
| `hda`, `ste`, `labor` | **texto** | Son códigos, no números. Ver *Gráficos*. |
| `hora_inicio`, `hora_final` | texto `HH:MM` | |
| `horometro_inicio`, `horometro_final`, `horometro_diferencia` | numérico | |
| `horas_trabajadas`, `area_trabajada` | numérico | Acepta coma o punto decimal. |
| `observaciones` | texto | |
| `registrado_en` | datetime | Llega en UTC; se resta 5 h fijas para hora de Colombia. |

Las columnas internas de la tabla (`id_registro`, `fecha_original`,
`telegram_chat_id`, `telegram_usuario`) se cargan pero nunca se muestran: la
lista `ORDEN_COLUMNAS` define qué se ve y en qué orden.

### Columnas derivadas

| Columna | Origen |
|---|---|
| `horas_calendario` | `hora_final − hora_inicio` en horas decimales; suma 24 si cruza medianoche. Es la referencia para contrastar `horas_trabajadas`. |
| `clave` | Concatenación de `ficha`, `fecha`, `hora_inicio` y `labor`. Identificador estable de una fila. |
| `actividad` | Nombre de la labor, resuelto contra la maestra. |
| `tiene_alerta`, `detalle_alertas` | Resumen de `marcar_alertas()`. |

### Traducción de labores

`labor` es el **código** de la actividad (p. ej. `3080115139`), no su nombre.
`agregar_actividades()` lo cruza contra `actividades` usando las columnas
`codigo` → `nome`.

Degradación deliberada: si la maestra no se puede leer o el código no existe,
la columna `actividad` conserva el código en lugar de quedar vacía, y la app
avisa. Nunca se pierde la fila por un problema de la maestra.

---

## Validaciones

`marcar_alertas()` devuelve un DataFrame booleano con una columna por regla.
`REGLAS` guarda la descripción que se muestra en la interfaz.

| Regla | Condición |
|---|---|
| Horómetro sin avance | `Δ ≤ 0` y `horas > 0` |
| Horómetro final menor al inicial | `horometro_final < horometro_inicio` |
| Avance de horómetro mayor a las horas | `Δ > horas + 1` |
| Horas no coinciden con el horario | `abs(horas − horas_calendario) > 0.5` |
| Sin área registrada | `area_trabajada ≤ 0` |

Las tolerancias (`+1` hora y `0.5` h) absorben redondeos de captura. La tercera
regla es la que detecta el error habitual de anotar kilometraje en el campo del
horómetro.

Para agregar una regla: añada la fila en `marcar_alertas()` **y** la entrada
correspondiente en `REGLAS`. La pestaña de calidad recorre `REGLAS`, así que una
regla sin descripción no se muestra.

---

## Gráficos

Dos constructores, ambos con el mismo tratamiento visual:

- `barras_horizontales()` — magnitudes por categoría.
- `barras_por_dia()` — serie temporal.

Decisiones que conviene no revertir sin motivo:

- **`type="category"` en el eje Y.** Los códigos de hacienda llegan como
  `'080104'`. Plotly infiere el tipo de eje en el navegador y, al ver cadenas
  con forma de número, las convierte a magnitudes (aparecía `82.5k` como marca
  de eje). Forzar el eje categórico lo evita.
- **`automargin=True`.** Los nombres de actividad son largos; sin esto se cortan.
- **Sin truncar etiquetas.** Dos nombres con el mismo prefijo colapsarían en una
  sola categoría y sumarían barras.

La paleta está en las constantes `AZUL`, `NARANJA`, `AQUA`, `ROJO`, `TINTA`,
`TINTA_SUAVE`, `SUPERFICIE` y `REJILLA`. Son valores validados para fondo claro;
el tema claro se fija en [.streamlit/config.toml](.streamlit/config.toml) para
que el navegador del usuario no altere el contraste.

---

## Puntos de extensión

| Quiero… | Dónde |
|---|---|
| Cambiar cómo se llama un campo en pantalla | `ETIQUETAS` |
| Mostrar u ocultar columnas del detalle, o reordenarlas | `ORDEN_COLUMNAS` |
| Agregar una validación | `marcar_alertas()` + `REGLAS` |
| Agregar un filtro | `multiselector()` en la barra lateral + el bucle que aplica los filtros |
| Cambiar colores | Las constantes de paleta y `.streamlit/config.toml` |
| Cambiar el formato de exportación | `a_excel()` / `a_csv()` |

---

## Limitaciones conocidas

- **Zona horaria fija.** `registrado_en` se ajusta restando 5 horas. Es correcto
  para Colombia, que no aplica horario de verano, pero rompería en otra zona.
- **Sólo lectura.** No hay forma de corregir un registro desde el tablero; las
  inconsistencias que muestra la pestaña de calidad se corrigen en el origen.
- **Estado no compartido.** Filtros y sesión viven en `st.session_state`, es
  decir por pestaña del navegador. Nada se persiste entre visitas.
- **Sin paginación en la interfaz.** Todo el conjunto filtrado se carga en
  memoria y se dibuja. Con decenas de miles de registros habría que paginar la
  consulta o agregar en la base.

---

## Archivos del proyecto

| Archivo | Contenido |
|---|---|
| [app.py](app.py) | La aplicación completa. |
| [requirements.txt](requirements.txt) | Dependencias. |
| [.streamlit/config.toml](.streamlit/config.toml) | Tema visual. |
| [.env.example](.env.example) | Plantilla de variables locales. |
| [.streamlit/secrets.toml.example](.streamlit/secrets.toml.example) | Plantilla de variables en la nube. |
#   L a b o r e s _ M a q u i n a r i a  
 