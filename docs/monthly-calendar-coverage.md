# Cobertura del mes completo

Contrato `monthly-event-coverage/v2`, aprobado por Jose el 2026-09-11
(`D-PROD-018`, `BUG-MONTH-001`).

Una misión mensual cubre todo su mes calendario en UTC. Septiembre incluye los
eventos desde el 1 de septiembre a las 00:00 UTC hasta antes del 1 de octubre a
las 00:00 UTC. Activar la configuración publica la misión y congela sus
parámetros; `activated_at` conserva su función de auditoría y no excluye eventos
anteriores del mismo mes.

El mes se determina con `card_data_v1.official_date`, con fallback a `date` y
`event_date`. Se rechaza un evento fechado en otro mes aunque el resumen declare
el mes de la misión. Por compatibilidad, si no hay una fecha resoluble se conserva
el mes declarado en el resumen. Los meses sin configurar o DRAFT no reciben
progreso. ACTIVE y CLOSED admiten resúmenes del mismo mes y correcciones.

MAIN EVENT ORACLE solo exige acertar el ganador del main event. Fallar el método
y el round no impide sumar un acierto: un evento así produce `1 / 3 headliners`
y 33% de progreso. No cambian el catálogo, las metas, las picks ni su puntuación.

## Recuperación del progreso omitido

Cambiar el filtro no vuelve a procesar resultados. El reconciliador habitual
omite las peleas/revisiones que ya tienen una marca de procesamiento y la UI
muestra el progreso mensual almacenado. Recuperar un evento omitido requiere
un recálculo acotado y autorizado por separado.

Jose autorizó el recálculo de septiembre para la cartelera de Hooker el
2026-09-11. El dry-run actualizado conservó el alcance revisado: dos usuarios,
dos registros mensuales nuevos, un usuario pasa a 1/3, el otro permanece en cero
y el delta de XP es cero.

La recuperación se aplicó en una única transacción con lectura snapshot y
confirmación majority, reutilizando el servicio mensual. Se guardaron respaldos
cifrados antes y después fuera del repositorio y se verificó que las entradas
no cambiaran entre el dry-run y la transacción. El adaptador de ejecución solo
permitió insertar/actualizar esos dos registros en `mission_monthly_progress`.
No se escribieron otras colecciones ni se cambiaron picks, resultados, XP,
fechas de activación o marcas de reconciliación. La verificación posterior,
de solo lectura, confirmó 1/3 (33%) para el acierto y 0/3 para el otro usuario.

Una falla antes del commit revierte la transacción completa. Después del commit,
la recuperación exige comparar los registros con las imágenes posteriores
cifradas y autorización separada antes de restaurar; nunca debe sobrescribir
progreso más reciente. Repetir el ejecutor con el plan antiguo se bloquea porque
ya existen los registros. El servicio mensual reemplaza el resumen por evento,
evitando duplicar su aporte y conservando la idempotencia de los premios de XP.

Validación: 139 pruebas del catálogo/configuración/progreso/orquestación, prueba
de septiembre repetida tras el ajuste final del fixture, Ruff de todo el backend
y una comprobación local de rollback y commit de los dos usuarios. El despliegue
del cambio de código se verifica por separado de esta recuperación de datos.
