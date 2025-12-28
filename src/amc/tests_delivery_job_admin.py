from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone
from amc.models import DeliveryJob, DeliveryJobTemplate, Cargo, DeliveryPoint
from datetime import timedelta

User = get_user_model()

class DeliveryJobAdminTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import UserManager
        from typing import cast
        self.user = cast(UserManager, User.objects).create_superuser(
            'admin', 'admin@example.com', 'password'
        )
        self.client.login(username='admin', password='password')
        
        self.cargo = Cargo.objects.create(key="test_cargo", label="Test Cargo")
        self.dp_source = DeliveryPoint.objects.create(guid="source", name="Source", type="Warehouse", coord="POINT(0 0 0)")
        self.dp_dest = DeliveryPoint.objects.create(guid="dest", name="Dest", type="Warehouse", coord="POINT(10 10 0)")

        self.template = DeliveryJobTemplate.objects.create(
            name="Test Template",
            default_quantity=100,
            bonus_multiplier=1.5,
            completion_bonus=10000,
            rp_mode=True,
            job_posting_probability=0.5,
            duration_hours=10.0
        )
        self.template.cargos.add(self.cargo)
        self.template.source_points.add(self.dp_source)
        self.template.destination_points.add(self.dp_dest)

    def test_admin_select_template_page_renders(self):
        url = reverse('admin:amc_deliveryjob_add')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin/amc/deliveryjob/select_template.html')
        self.assertContains(response, "Test Template")

    def test_admin_create_from_template_prefills_form(self):
        url = reverse('admin:amc_deliveryjob_add')
        response = self.client.get(url, {'template': self.template.id})
        self.assertEqual(response.status_code, 200)
        # Check standard admin add form is rendered
        self.assertTemplateUsed(response, 'admin/change_form.html')
        
        # Verify initial data in context admin form
        admin_form = response.context['adminform']
        initial = admin_form.form.initial
        
        self.assertEqual(initial['name'], "Test Template")
        self.assertEqual(initial['quantity_requested'], 100)
        self.assertEqual(initial['bonus_multiplier'], 1.5)
        self.assertEqual(initial['completion_bonus'], 10000)
        self.assertEqual(initial['rp_mode'], True)
        self.assertEqual(initial['created_from'], self.template)
        
        # Verify M2M prefill (might need to check form field values directly if initial doesn't strictly cover it in all admin versions, but initial update in get_changeform_initial_data should work)
        # However, for ModelMultipleChoiceField, initial should be a list of PKs or QuerySet.
        self.assertIn(self.cargo, initial['cargos'])
        self.assertIn(self.dp_source, initial['source_points'])

        # Verify expired_at calculation
        expected_expiry = timezone.now() + timedelta(hours=self.template.duration_hours)
        # Allow 5 second delta
        self.assertAlmostEqual(initial['expired_at'], expected_expiry, delta=timedelta(seconds=5))

    def test_admin_create_from_scratch(self):
        url = reverse('admin:amc_deliveryjob_add')
        response = self.client.get(url, {'scratch': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin/change_form.html')
        
        admin_form = response.context['adminform']
        initial = admin_form.form.initial
        # Should be mostly empty or default
        self.assertNotIn('quantity_requested', initial)

    def test_create_iob_functionality(self):
        # Emulate POST to create a job
        url = reverse('admin:amc_deliveryjob_add')
        data = {
            'name': "New Job from Template",
            'quantity_requested': 200,
            'quantity_fulfilled': 0,
            'bonus_multiplier': 1.2,
            'completion_bonus': 5000,
            'rp_mode': 'on', # Checkbox
            'created_from': self.template.id,
            'cargos': [self.cargo.key],
            'source_points': [self.dp_source.guid],
            'destination_points': [self.dp_dest.guid],
            'expired_at_0': '2025-12-31',
            'expired_at_1': '12:00:00',
        }
        response = self.client.post(url, data)
        if response.status_code != 302:
            print(response.content.decode())
        self.assertEqual(response.status_code, 302) # Redirects to list view
        
        job = DeliveryJob.objects.last()
        self.assertEqual(job.name, "New Job from Template")
        self.assertEqual(job.created_from, self.template)
        self.assertTrue(job.rp_mode)

    def test_invalid_template_id(self):
        """Verify handling of non-existent template ID in GET param."""
        url = reverse('admin:amc_deliveryjob_add')
        # Pass a huge ID that shouldn't exist
        response = self.client.get(url, {'template': 999999})
        self.assertEqual(response.status_code, 200)
        # Should fall back to standard add view without crashing
        self.assertTemplateUsed(response, 'admin/change_form.html')
        # Initial data should be empty/default since template wasn't found
        admin_form = response.context['adminform']
        self.assertNotIn('name', admin_form.form.initial)

    def test_template_deletion_behavior(self):
        """Verify SET_NULL behavior when template is deleted."""
        # Create a job linked to the template
        job = DeliveryJob.objects.create(
            name="Job Linked to Template",
            created_from=self.template,
            quantity_requested=100,
            bonus_multiplier=1.0,
            completion_bonus=100
        )
        self.assertEqual(job.created_from, self.template)
        
        # Delete the template
        self.template.delete()
        
        # Refresh job from DB
        job.refresh_from_db()
        self.assertIsNone(job.created_from)

    def test_permissions_check(self):
        """Verify non-staff cannot access the add view."""
        self.client.logout()
        url = reverse('admin:amc_deliveryjob_add')
        response = self.client.get(url)
        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        # Type safe access or ignore
        self.assertIn('/admin/login/', getattr(response, 'url', ''))
