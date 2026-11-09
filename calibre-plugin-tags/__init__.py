from calibre.customize import InterfaceActionBase


class SynchronizeTagsPlugin(InterfaceActionBase):

    name = "Synchronize Tags"
    description = "Synchronize Tags"
    supported_platforms = ["windows", "osx", "linux"]
    author = "Dunderlab"
    version = (1, 0, 0)
    minimum_calibre_version = (5, 0, 0)

    actual_plugin = "calibre_plugins.synchronize_tags.ui_tags:SynchronizeTags"
