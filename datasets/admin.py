from django.contrib import admin
from .models import Dataset, DatasetFile, Hit
from guardian.admin import GuardedModelAdmin


# class DatasetAdmin(admin.ModelAdmin):
class DatasetAdmin(GuardedModelAdmin):
    list_display = ('id', 'label', 'title', 'create_date', 'datatype', 'ds_status')


# class DatasetFileAdmin(admin.ModelAdmin):
class DatasetFileAdmin(GuardedModelAdmin):
    list_display = ('dataset_id_id', 'file', 'upload_date', 'df_status', 'format', 'datatype')


@admin.register(Hit)
class HitAdmin(admin.ModelAdmin):
    list_display = ('id', 'query_pass', 'reviewed', 'task_id', 'authrecord_id', 'relation_type', 'src_id')


admin.site.register(DatasetFile, DatasetFileAdmin)
admin.site.register(Dataset, DatasetAdmin)
