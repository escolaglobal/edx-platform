"""
"""
from cStringIO import StringIO

from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image

from ..user_api.accounts.helpers import get_profile_image_storage, get_profile_image_name, get_profile_image_filename, PROFILE_IMAGE_SIZES


class DEV_MSG(object):
    """
    Holder for pseudo-constants.
    """
    FILE_TOO_LARGE = 'Maximum file size exceeded.'
    FILE_TOO_SMALL = 'Minimum file size not met.'
    FILE_BAD_TYPE = 'Unsupported file type.'
    FILE_BAD_EXT = 'File extension does not match data.'
    FILE_BAD_MIMETYPE = 'Content-Type header does not match data.'


class ImageFileRejected(Exception):
    """
    Exception to use when the system rejects a user-uploaded source image.
    """
    pass


def validate_uploaded_image(image_file, content_type):
    """
    Raises an InvalidProfileImage if the server should refuse to use this
    uploaded file as the source image for a user's profile image.  Otherwise,
    returns nothing.
    """
    # TODO: better to just use PIL for this?  seems like it

    image_types = {
        'jpeg' : {
            'extension': [".jpeg", ".jpg"],
            'mimetypes': ['image/jpeg', 'image/pjpeg'],
            'magic': ["ffd8"]
            },
        'png': {
            'extension': [".png"],
            'mimetypes': ['image/png'],
            'magic': ["89504e470d0a1a0a"]
            },
        'gif': {
            'extension': [".gif"],
            'mimetypes': ['image/gif'],
            'magic': ["474946383961", "474946383761"]
            }
        }

    # check file size
    print 'size', image_file.size
    if image_file.size > settings.PROFILE_IMAGE_MAX_BYTES:
        raise ImageFileRejected(DEV_MSG.FILE_TOO_LARGE)
    elif image_file.size < settings.PROFILE_IMAGE_MIN_BYTES:
        raise ImageFileRejected(DEV_MSG.FILE_TOO_SMALL)

    # check the file extension looks acceptable
    filename = str(image_file.name).lower()
    filetype = [ft for ft in image_types if any(filename.endswith(ext) for ext in image_types[ft]['extension'])]
    if not filetype:
        raise ImageFileRejected(DEV_MSG.FILE_BAD_TYPE)
    filetype = filetype[0]

    # check mimetype matches expected file type
    if content_type not in image_types[filetype]['mimetypes']:
        raise ImageFileRejected(DEV_MSG.FILE_BAD_MIMETYPE)

    # check image file headers match expected file type
    headers = image_types[filetype]['magic']
    if image_file.read(len(headers[0])/2).encode('hex') not in headers:
        raise ImageFileRejected(DEV_MSG.FILE_BAD_EXT)
    # avoid unexpected errors from subsequent modules expecting the fp to be at 0
    image_file.seek(0)
    return filetype


def _get_scaled_image_file(image_obj, size):
    """
    Given a PIL.Image object, get a copy resized using `size` (square) and
    return a file-like object containing the data saved as a JPEG.

    Note that the file object returned is a django ContentFile which exists
    only in memory (not on disk).
    """
    scaled = image_obj.resize((size, size), Image.ANTIALIAS)
    string_io = StringIO()
    scaled.save(string_io, format='JPEG')
    image_file = ContentFile(string_io.getvalue())
    return image_file


def _store_profile_image(image_file, size, username):
    """
    Permanently store the contents of the uploaded_file as this user's profile
    image, in whatever storage backend we're configured to use.  Any
    previously-stored profile image will be overwritten.

    Returns the path to the stored file.
    """
    storage = get_profile_image_storage()
    name = get_profile_image_name(username)
    dest_name = get_profile_image_filename(name, size)
    # TODO overwrites should be atomic, but FileStorage doesn't support this.
    if storage.exists(dest_name):
        storage.delete(dest_name)
    print storage.save(dest_name, image_file)


def generate_profile_images(image_file, username):
    """
    """
    image_obj = Image.open(image_file)

    # first center-crop the image if needed (but no scaling yet).
    width, height = image_obj.size
    if width != height:
        side = width if width < height else height
        image_obj = image_obj.crop(((width-side)/2, (height-side)/2, (width+side)/2, (height+side)/2))

    for side in [30, 50, 120, 500]:
        scaled_image_file = _get_scaled_image_file(image_obj, side)
        # Store the file.
        _store_profile_image(scaled_image_file, side, username)


def remove_profile_images(username):
    """
    """
    storage = get_profile_image_storage()
    name = get_profile_image_name(username)
    for size in PROFILE_IMAGE_SIZES.values():
        dest_name = get_profile_image_filename(name, size)
        storage.delete(dest_name)


