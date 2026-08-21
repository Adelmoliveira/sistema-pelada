SPORTS_MATERIAL_CATEGORY = "Material Esportivo"


def is_sports_material(category):
    return category == SPORTS_MATERIAL_CATEGORY


def validate_single_department(products):
    """Return ``sports`` or ``bar`` and reject mixed product collections."""
    departments = {
        "sports" if is_sports_material(product["category"]) else "bar"
        for product in products
    }
    if len(departments) != 1:
        raise ValueError("Produtos do Bar e Material Esportivo não podem estar no mesmo pedido.")
    return next(iter(departments))


def validate_requested_department(products, requested_department):
    """Validate that every item belongs to the department selected at checkout."""
    if requested_department not in {"bar", "sports"}:
        raise ValueError("Departamento da compra inválido.")
    actual_department = validate_single_department(products)
    if actual_department != requested_department:
        raise ValueError("Os produtos não pertencem ao departamento selecionado.")
    return actual_department
