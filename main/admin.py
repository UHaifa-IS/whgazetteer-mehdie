from django.contrib import admin
from .models import Log, Comment


@admin.register(Log)
class LogAdmin(admin.ModelAdmin):
    pass


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    pass
