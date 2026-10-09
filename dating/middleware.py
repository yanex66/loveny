from django.shortcuts import render
from django.urls import reverse
from dating.models import SiteConfiguration

class MaintenanceModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Allow access to admin even in maintenance mode
        if request.path.startswith(reverse('admin:index')):
            return self.get_response(request)

        try:
            config = SiteConfiguration.get_solo()
            if config.is_maintenance_mode:
                # If it's an API request (like /api/ or JSON request), we might want to return JSON,
                # but for simplicity, we render a maintenance template or return a response.
                if request.headers.get('accept') == 'application/json' or request.path.startswith('/api/'):
                    from django.http import JsonResponse
                    return JsonResponse({'error': 'maintenance_mode', 'message': config.maintenance_message}, status=503)
                
                return render(request, 'maintenance.html', {'message': config.maintenance_message}, status=503)
        except Exception:
            pass
        
        response = self.get_response(request)
        return response

