from .models import SiteConfiguration


def site_configuration_context(request):
    """
    Exposes `site_config` globally to all templates.
    """
    try:
        config = SiteConfiguration.get_solo()
    except Exception:
        config = None
    return {
        'site_config': config,
    }
