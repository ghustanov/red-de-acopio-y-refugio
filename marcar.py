"""
Panel de moderación de desaparecidos.
Marca a una persona como: 🟡 Buscando · ✅ Apareció · 🕯️ Fallecida
y regenera la página automáticamente.

Uso:
    python3 marcar.py
"""
import db
import build_site

ETIQUETAS = {"buscando": "🟡 Buscando", "aparecio": "✅ Apareció", "fallecida": "🕯️ Fallecida"}
OPCIONES = {"1": "buscando", "2": "aparecio", "3": "fallecida"}


def main():
    db.init()
    personas = db.listar_desaparecidos()
    if not personas:
        print("No hay desaparecidos registrados todavía.")
        return

    print("\n=== DESAPARECIDOS ===")
    for i, p in enumerate(personas, 1):
        est = p["estado_persona"] or "buscando"
        print(f"  {i:>2}. {ETIQUETAS.get(est, est)}   {p['titulo']}")
    print()

    try:
        entrada = input("¿Número de la persona a actualizar? (Enter para salir) ").strip()
    except EOFError:
        return
    if not entrada:
        print("Saliendo.")
        return
    try:
        sel = int(entrada)
        assert 1 <= sel <= len(personas)
    except (ValueError, AssertionError):
        print("Número inválido.")
        return

    persona = personas[sel - 1]
    print(f"\nSeleccionaste: {persona['titulo']}")
    print("  1) 🟡 Buscando    2) ✅ Apareció    3) 🕯️ Fallecida")
    op = input("Nuevo estado (1/2/3)? ").strip()
    if op not in OPCIONES:
        print("Opción inválida.")
        return

    db.marcar_persona(persona["tg_id"], OPCIONES[op])
    salida, _ = build_site.construir()
    print(f"\n✅ '{persona['titulo']}' → {ETIQUETAS[OPCIONES[op]]}")
    print(f"🌐 Página actualizada: {salida}")


if __name__ == "__main__":
    main()
