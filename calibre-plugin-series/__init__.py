from calibre.customize import InterfaceActionBase


class SynchronizeSeriesPlugin(InterfaceActionBase):

    name = "Synchronize Series"
    description = "Synchronize Series"
    supported_platforms = ["windows", "osx", "linux"]
    author = "Dunderlab"
    version = (1, 0, 0)
    minimum_calibre_version = (5, 0, 0)

    actual_plugin = "calibre_plugins.synchronize_series.ui:SynchronizeSeries"
