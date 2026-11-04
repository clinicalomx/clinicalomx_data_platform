from django.apps import AppConfig


class ClinicalomxLedgerConfig(AppConfig):
    # Table names derive from the label, so it is fixed explicitly. Never change it.
    name = "clinicalomx_ledger"
    label = "clinicalomx_ledger"
    verbose_name = "clinicalOMx governance ledger"
    default_auto_field = "django.db.models.BigAutoField"
