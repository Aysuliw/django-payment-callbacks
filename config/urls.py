from django.urls import path

from payments import views

urlpatterns = [
    path("orders/", views.create_order),
    path("payments/callback/", views.payment_callback),
]
