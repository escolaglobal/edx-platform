"""

POST /uploads

* 'file' must be set
* 'file' must have acceptable mime type
* 'file' must have acceptable extension
* 'file' must be within acceptable size range

* authentication
* authorization

* response content...
* response structure...

"""
import os
from tempfile import NamedTemporaryFile

import ddt
from django.core.urlresolvers import reverse
import mock
from PIL import Image
from rest_framework.test import APITestCase, APIClient

from student.tests.factories import UserFactory

from ..views import DEV_MSG_FILE_TOO_LARGE, DEV_MSG_FILE_TOO_SMALL, DEV_MSG_FILE_BAD_TYPE, DEV_MSG_FILE_BAD_EXT, DEV_MSG_FILE_BAD_MIMETYPE, name_profile_image, get_profile_image_storage

TEST_PASSWORD = "test"


@ddt.ddt
class ProfileImageUploadTestCase(APITestCase):

    def setUp(self):
        super(ProfileImageUploadTestCase, self).setUp()
        self.anonymous_client = APIClient()
        self.different_user = UserFactory.create(password=TEST_PASSWORD)
        self.different_client = APIClient()
        self.staff_user = UserFactory(is_staff=True, password=TEST_PASSWORD)
        self.staff_client = APIClient()
        self.user = UserFactory.create(password=TEST_PASSWORD)
        self.url = reverse("profile_image_upload", kwargs={'username': self.user.username})
        self.storage = get_profile_image_storage()
        self.storage.delete(name_profile_image(self.user.username, '30'))
        self.storage.delete(name_profile_image(self.user.username, '50'))
        self.storage.delete(name_profile_image(self.user.username, '120'))
        self.storage.delete(name_profile_image(self.user.username, '500'))

    def tearDown(self):
        self.storage.delete(name_profile_image(self.user.username, '30'))
        self.storage.delete(name_profile_image(self.user.username, '50'))
        self.storage.delete(name_profile_image(self.user.username, '120'))
        self.storage.delete(name_profile_image(self.user.username, '500'))

    def test_anonymous_access(self):
        """
        Test that an anonymous client (not logged in) cannot call GET or POST.
        """
        for request in (self.anonymous_client.get, self.anonymous_client.post):
            response = request(self.url)
            self.assertEqual(401, response.status_code)

    def _make_image_file(self, dimensions=(320, 240), extension=".jpeg", force_size=None):
        """
        Returns a named temporary file created with the specified image type and options.

        Note the default dimensions are unequal (not a square) to ensure the center-square
        cropping logic will be exercised.
        """
        image = Image.new('RGB', dimensions, "green")
        image_file = NamedTemporaryFile(suffix=extension)
        image.save(image_file)
        if force_size is not None:
            image_file.seek(0, os.SEEK_END)
            bytes_to_pad = force_size - image_file.tell()
            # write in hunks of 256 bytes
            hunk, byte_ = bytearray([0] * 256), bytearray([0])
            num_hunks, remainder = divmod(bytes_to_pad, 256)
            for _ in xrange(num_hunks):
                image_file.write(hunk)
            for _ in xrange(remainder):
                image_file.write(byte_)
            image_file.flush()
        image_file.seek(0)
        return image_file

    def _get_thumbnail_names(self, username):
        """
        Return a dict with {size: filename} for each thumbnail
        """
        return {dimension: name_profile_image(username, str(dimension)) for dimension in (30, 50, 120, 500)}

    def assert_thumbnails(self, exist=True):
        """
        """
        for size, name in self._get_thumbnail_names(self.user.username).items():
            if exist:
                self.assertTrue(self.storage.exists(name))
                img = Image.open(self.storage.path(name))
                self.assertEqual(img.size, (size, size))
                self.assertEqual(img.format, 'JPEG')
            else:
                self.assertFalse(self.storage.exists(name))

    def test_upload_self(self):
        """
        Test that an authenticated user can POST to their own upload endpoint.
        """
        self.client.login(username=self.user.username, password=TEST_PASSWORD)
        response = self.client.post(self.url, {'file': self._make_image_file()}, format='multipart')
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "success"}, response.data)
        self.assert_thumbnails()

    def test_upload_other(self):
        """
        Test that an authenticated user cannot POST to another user's upload endpoint.
        """
        self.different_client.login(username=self.different_user.username, password=TEST_PASSWORD)
        response = self.different_client.post(self.url, {'file': self._make_image_file()}, format='multipart')
        self.assertEqual(403, response.status_code)
        self.assert_thumbnails(False)

    def test_upload_staff(self):
        """
        Test that an authenticated staff user can POST to another user's upload endpoint.
        """
        self.staff_client.login(username=self.staff_user.username, password=TEST_PASSWORD)
        response = self.staff_client.post(self.url, {'file': self._make_image_file()}, format='multipart')
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "success"}, response.data)
        self.assert_thumbnails(True)

    def test_upload_missing_file(self):
        """
        Test that omitting the file entirely from the POST results in HTTP 400.
        """
        self.client.login(username=self.user.username, password=TEST_PASSWORD)
        response = self.client.post(self.url, {}, format='multipart')
        self.assertEqual(400, response.status_code)
        self.assert_thumbnails(False)

    def test_upload_not_a_file(self):
        """
        Test that sending unexpected data that isn't a file results in HTTP 400.
        """
        self.client.login(username=self.user.username, password=TEST_PASSWORD)
        response = self.client.post(self.url, {'file': 'not a file'}, format='multipart')
        self.assertEqual(400, response.status_code)
        self.assert_thumbnails(False)

    def test_upload_file_too_large(self):
        """
        """
        image_file = self._make_image_file(force_size=(1024 * 1024) + 1)  # TODO settings / override settings
        self.client.login(username=self.user.username, password=TEST_PASSWORD)
        response = self.client.post(self.url, {'file': image_file}, format='multipart')
        self.assertEqual(400, response.status_code)
        self.assertEqual(response.data.get('developer_message'), DEV_MSG_FILE_TOO_LARGE)
        self.assert_thumbnails(False)

    def test_upload_file_too_small(self):
        """
        """
        image_file = self._make_image_file(dimensions=(1, 1), extension=".png", force_size=99)  # TODO settings / override settings
        self.client.login(username=self.user.username, password=TEST_PASSWORD)
        response = self.client.post(self.url, {'file': image_file}, format='multipart')
        self.assertEqual(400, response.status_code)
        self.assertEqual(response.data.get('developer_message'), DEV_MSG_FILE_TOO_SMALL)
        self.assert_thumbnails(False)

    def test_upload_bad_extension(self):
        """
        """
        self.client.login(username=self.user.username, password=TEST_PASSWORD)
        response = self.client.post(self.url, {'file': self._make_image_file(extension=".bmp")}, format='multipart')
        self.assertEqual(400, response.status_code)
        self.assertEqual(response.data.get('developer_message'), DEV_MSG_FILE_BAD_TYPE)
        self.assert_thumbnails(False)

    # ext / header mismatch
    def test_upload_wrong_extension(self):
        """
        """
        self.client.login(username=self.user.username, password=TEST_PASSWORD)
        # make a bmp, rename it to jpeg
        bmp_file = self._make_image_file(extension=".bmp")
        fake_jpeg_file = NamedTemporaryFile(suffix=".jpeg")
        fake_jpeg_file.write(bmp_file.read())
        fake_jpeg_file.seek(0)
        response = self.client.post(self.url, {'file': fake_jpeg_file}, format='multipart')
        self.assertEqual(400, response.status_code)
        self.assertEqual(response.data.get('developer_message'), DEV_MSG_FILE_BAD_EXT)
        self.assert_thumbnails(False)

    # content-type / header mismatch
    @mock.patch('django.test.client.mimetypes')
    def test_upload_bad_content_type(self, mock_mimetypes):
        """
        """
        mock_mimetypes.guess_type.return_value = ['image/gif']
        self.client.login(username=self.user.username, password=TEST_PASSWORD)
        response = self.client.post(self.url, {'file': self._make_image_file(extension=".jpeg")}, format='multipart')
        self.assertEqual(400, response.status_code)
        self.assertEqual(response.data.get('developer_message'), DEV_MSG_FILE_BAD_MIMETYPE)
        self.assert_thumbnails(False)

    @ddt.data(
        (1, 1), (10, 10), (100, 100), (1000, 1000),
        (1, 10), (10, 100), (100, 1000), (1000, 999)
    )
    def test_resize(self, size):
        """
        use a variety of input image sizes to ensure that the output pictures
        are all properly scaled
        """
        self.client.login(username=self.user.username, password=TEST_PASSWORD)
        response = self.client.post(self.url, {'file': self._make_image_file(size)}, format='multipart')
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "success"}, response.data)
        self.assert_thumbnails()
