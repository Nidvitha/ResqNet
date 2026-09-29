from django.conf import settings


def carto_basemaps(request):
    return {"CARTO_BASEMAPS_API_KEY": settings.CARTO_BASEMAPS_API_KEY}