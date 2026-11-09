from calibre.customize import InterfaceActionBase


class SynchronizeSeriesAndTagsPlugin(InterfaceActionBase):

    name = "Synchronize Series and Tags"
    description = "Synchronize Series and Tags"
    supported_platforms = ["windows", "osx", "linux"]
    author = "Dunderlab"
    version = (1, 0, 0)
    minimum_calibre_version = (5, 0, 0)

    actual_plugin = (
        "calibre_plugins.synchronize_series_and_tags.ui:SynchronizeSeriesAndTags"
    )
