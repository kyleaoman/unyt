"""
a dask array class (unyt_dask_array) and helper functions for unyt.



"""

import unyt.array as ua
from numpy import ndarray
from dask.array.core import Array, finalize
from functools import wraps
from unyt.unit_object import _get_conversion_factor

# the following attributes hang off of dask.array.core.Array and do not modify units
_use_simple_decorator = [
    "min",
    "max",
    "sum",
    "mean",
    "std",
    "cumsum",
    "squeeze",
    "rechunk",
    "clip",
    "view",
    "swapaxes",
    "round",
    "copy",
    "__deepcopy__",
    "repeat",
    "astype",
    "reshape",
    "topk",
]


def _simple_dask_decorator(dask_func, current_unyt_dask):
    """
    a decorator for the simpler dask functions that can just copy over the current
    unit info after applying the dask function. this includes functions that return
    single values (e.g., min()) or functions that change the array in ways that do
    not affect units like reshaping or rounding.

    Parameters
    ----------

    dask_func: func handle
       the dask function handle to call
    current_unyt_dask: unyt_dask_array
       the current instance of a unyt_dask_array

    Returns a new unyt_dask_array instance with appropriate units
    """
    @wraps(dask_func)
    def wrapper(*args, **kwargs):
        da = dask_func(*args, **kwargs)  # will return standard dask array
        return _create_with_quantity(da, current_unyt_dask._unyt_quantity)

    return wrapper

# the following list are the unyt_quantity/array attributes that will also
# be attributes of the unyt_dask_array. These methods are wrapped with a unit
# conversion decorator.
_unyt_funcs_to_track = [
    "to",
    "in_units",
    "in_cgs",
    "in_base",
    "in_mks"
]


def _track_conversion(unyt_func_name, current_unyt_dask):
    """
    a decorator to use with unyt functions that convert units

    Parameters
    ----------

    unyt_func_name: str
        the name of the function to call from the sidecar _unyt_quantity. Must be
        an attribute of unyt.unyt_quantity.
    current_unyt_dask: unyt_dask_array
        the current instance of a unyt_dask_array


    Returns a new unyt_dask_array instance with appropriate units
    """
    def wrapper(*args, **kwargs):

        # current value of sidecar quantity
        init_val = current_unyt_dask._unyt_quantity.value

        # get the unyt function handle and call it
        the_func = getattr(current_unyt_dask._unyt_quantity, unyt_func_name)
        new_unyt_quantity = the_func(*args, **kwargs)

        # calculate the conversion factor and pull out the new name and units
        # might be able to use _get_conversion_factor here too...
        factor = new_unyt_quantity.value / init_val
        units = new_unyt_quantity.units

        # apply the factor, return new
        new_obj = unyt_from_dask(current_unyt_dask * factor, units)

        return new_obj
    # functools wrap fails here because unyt_func_name is a string, copy manually:
    wrapper.__doc__ = getattr(current_unyt_dask._unyt_quantity, unyt_func_name).__doc__
    return wrapper


# helper sanitizing functions for handling ufunc, array operations
def _extract_unyt(obj):
    # returns the hidden _unyt_quantity if it exists in obj, otherwise return obj
    return obj._unyt_quantity if isinstance(obj, unyt_dask_array) else obj


def _extract_dask(obj):
    # returns a plain dask array if the obj is a unyt_dask_array, otherwise return obj
    return obj.to_dask() if isinstance(obj, unyt_dask_array) else obj


def _extract_unyt_val(obj):
    # returns the value of a unyt_quantity if obj is a unyt_quantity
    return obj.to_value() if isinstance(obj, ua.unyt_quantity) else obj


def _sanitize_unit_args(*input):
    # returns sanitized inputs and unyt_inputs for calling the ufunc
    unyt_inputs = [_extract_unyt(i) for i in input]

    if len(unyt_inputs) > 1:
        # note 1: even though we rely on the ufunc applied to the unyt quantities
        # to get the final units of our unyt_dask_array after an operation,
        # we need to first ensure that if our arguments have the same dimensions
        # they are in the same units. This happens internally for unyt_quantities
        # but we also need to apply those internal unit conversions to our
        # dask_unyt_array objects, so we do those checks manually here.
        # note 2: we do NOT check for validity of the operation here. The subsequent
        # call to the ufunc with the unyt_inputs will enforce the unyt rules
        # (e.g., addition must have same dimensions).
        ui_0, ui_1 = unyt_inputs[0], unyt_inputs[1]
        if (hasattr(ui_0, 'units') and
            hasattr(ui_1, 'units') and
            ui_0.units != ui_1.units and
            ui_0.units.dimensions == ui_1.units.dimensions):

            # convert to the unit with the larger base
            input = list(input)
            if ui_0.units.base_value < ui_1.units.base_value:
                input[0] = input[0].to(ui_1.units)
            else:
                input[1] = input[1].to(ui_0.units)
            unyt_inputs = [_extract_unyt(i) for i in input]

    return input, unyt_inputs

def _prep_ufunc(ufunc, *input, extract_dask=False, **kwargs):
    # this function:
    # (1) sanitizes inputs for calls to __array_func__, __array__ and _elementwise
    # (2) applies the function to the hidden unyt quantities
    # (3) (optional) makes inputs extra clean: converts unyt_dask_array args to plain dask array objects

    # apply the operation to the hidden unyt_quantities
    input, unyt_inputs = _sanitize_unit_args(*input)
    unyt_result = ufunc(*unyt_inputs, **kwargs)

    if extract_dask:
        input = [_extract_dask(i) for i in input]

    input = [_extract_unyt_val(i) for i in input]
    return input, unyt_result


def _post_ufunc(dask_superfunk, unyt_result):
    # a decorator to attach hidden unyt quantity to result of a ufunc, array or elemwise calculation
    def wrapper(*args, **kwargs):
        dask_result = dask_superfunk(*args, **kwargs)
        if hasattr(unyt_result, 'units'):
            return _create_with_quantity(dask_result, unyt_result)
        return dask_result
    return wrapper


def _special_dec(the_func):
    # decorator for special operations like __mul__ , __truediv__
    def wrapper(*args, **kwargs):
        funcname = the_func.__name__
        ufunc = getattr(ua.unyt_quantity, funcname)
        newargs, unyt_result = _prep_ufunc(ufunc, *args, extract_dask=True, **kwargs)

        dasksuperfunk = getattr(Array, funcname)
        daskresult = dasksuperfunk(*newargs, **kwargs)

        if hasattr(unyt_result, 'units'):
            return _create_with_quantity(daskresult, unyt_result)
        return daskresult
    return wrapper

# note: the unyt_dask_array class has no way of catching daskified reductions (yet?).
# operations like dask.array.min() get routed through dask.array.reductions.min()
# and will return plain arrays or float/int values. When these operations exist as
# attributes, they can be called and will return unyt objects. i.e., :
# import dask; import unyt
# x_da = unyt_from_dask(dask.array.ones((10, 10), chunks=(2, 2)), unyt.m)
# dask.array.min(x_da).compute()  #  returns a plain float
# x_da.min().compute()  #  returns a unyt quantity


class unyt_dask_array(Array):
    """
    a dask.array.core.Array subclass that tracks units. Easiest to use the
    unyt_from_dask helper function to generate new instances.

    Parameters
    ----------

    All parameters are those for dask.array.core.Array

    """

    def __new__(
        clss,
        dask_graph,
        name,
        chunks,
        dtype=None,
        meta=None,
        shape=None,
        units=None,
        registry=None,
        bypass_validation=False,
        input_units=None,
        unyt_name=None,
    ):

        # get the base dask array
        obj = super(unyt_dask_array, clss).__new__(
            clss,
            dask_graph,
            name,
            chunks,
            dtype,
            meta,
            shape,
        )

        # attach our unyt sidecar quantity
        dtype = obj.dtype
        obj._unyt_quantity = ua.unyt_quantity(
            1.0,
            units,
            registry,
            dtype,
            bypass_validation,
            input_units,
            unyt_name,
        )

        obj.units = obj._unyt_quantity.units
        obj.unyt_name = obj._unyt_quantity.name

        # set the unit conversion attributes so they are discoverable. no name
        # conflicts for now, but this could be an issue if _unyt_funcs_to_track
        # is expanded.
        for attr in _unyt_funcs_to_track:
            setattr(obj, attr, getattr(obj._unyt_quantity, attr))

        return obj

    def _elemwise(self, ufunc, *args, **kwargs):
        args, unyt_result = _prep_ufunc(ufunc, *args, **kwargs)
        return _post_ufunc(super()._elemwise, unyt_result)(ufunc, *args, **kwargs)

    def __array_ufunc__(self, numpy_ufunc, method, *inputs, **kwargs):
        inputs, unyt_result = _prep_ufunc(numpy_ufunc, *inputs, extract_dask=True, **kwargs)
        return _post_ufunc(super().__array_ufunc__, unyt_result)(numpy_ufunc, method, *inputs, **kwargs)

    def __array_function__(self, func, types, args, kwargs):
        args, unyt_result = _prep_ufunc(func, *args, extract_dask=True, **kwargs)
        types = [type(i) for i in args]
        return _post_ufunc(super().__array_function__, unyt_result)(func, types, args, kwargs)

    def __repr__(self):
        disp_str = super().__repr__().replace('dask.array', 'unyt_dask_array')
        units_str = f", units={self.units.__str__()}>"
        return disp_str.replace(">", units_str)

    def to_dask(self):
        """ return a plain dask array. Only copies high level graphs, should be cheap...
        """
        (cls, args) = self.__reduce__()
        return super().__new__(Array, *args)

    def __getattribute__(self, name):
        # huh, add ends up here. unyt_quantity(blah) + unyt_dask_instance. but not
        # unyt_quantity(blah) + unyt_dask_instance
        if name in _unyt_funcs_to_track:
            return _track_conversion(name, self)

        result = super().__getattribute__(name)
        if name in _use_simple_decorator:
            return _simple_dask_decorator(result, self)

        return result

    def __dask_postcompute__(self):
        # a dask hook to catch after .compute(), see
        # https://docs.dask.org/en/latest/custom-collections.html#example-dask-collection
        return _finalize_unyt, ((self.units, ))

    def _set_unit_state(self, units, new_unyt_quantity, unyt_name):
        # sets just the unit state of the object
        self.units = units
        self._unyt_quantity = new_unyt_quantity
        self.unyt_name = unyt_name

    # These methods bypass __getattribute__ and numpy hooks, so they are defined
    # explicitly here (but are handled generically by the _special_dec decorator).

    @_special_dec
    def __abs__(self): pass

    @_special_dec
    def __pow__(self, other): pass

    @_special_dec
    def __mul__(self, other): pass

    @_special_dec
    def __rmul__(self, other): pass

    @_special_dec
    def __div__(self, other): pass

    @_special_dec
    def __rdiv__(self, other): pass

    @_special_dec
    def __truediv__(self, other): pass

    @_special_dec
    def __rtruediv__(self, other): pass

    @_special_dec
    def __add__(self, other): pass

    @_special_dec
    def __radd__(self, other): pass

    @_special_dec
    def __sub__(self, other): pass

    @_special_dec
    def __rsub__(self, other): pass


def _finalize_unyt(results, unit_name):
    """
    the function to call from the __dask_postcompute__ hook.

    Parameters
    ----------
    results : the dask results object
    unit_name : the units of the result

    Returns
    -------
    unyt_array or unyt_quantity

    """

    # here, we first call the standard finalize function for a dask array
    # and then return a standard unyt_array from the now in-memory result if
    # the result is an array, otherwise return a unyt_quantity.
    result = finalize(results)

    if type(result) == ndarray:
        return ua.unyt_array(result, unit_name)
    else:
        return ua.unyt_quantity(result, unit_name)


def _create_with_quantity(dask_array, new_unyt_quantity):
    """
    this function instantiates a new unyt_dask_array instance and then sets
    the unit state, including units. Used to wrap dask operations

    Parameters
    ----------
    dask_array : a standard dask array
    new_unyt_quantity : a standard unity quantity
    remaining arguments get passed to unyt.unyt_array, check there for a
    description.
    """
    out = unyt_from_dask(dask_array)

    # attach the unyt_quantity
    units = new_unyt_quantity.units
    unyt_name = new_unyt_quantity.name

    out._set_unit_state(units, new_unyt_quantity, unyt_name)
    return out


def unyt_from_dask(
    dask_array,
    units=None,
    registry=None,
    bypass_validation=False,
    unyt_name=None,
):
    """
    creates a unyt_dask_array from a standard dask array.

    Parameters
    ----------
    dask_array : a standard dask array

    remaining arguments get passed to unyt.unyt_array, check there for a
    description.

    Examples
    --------

    >>> from unyt import dask_array
    >>> import dask.array as da
    >>> x = da.random.random((10000, 10000), chunks=(1000, 1000))
    >>> x_da = dask_array.unyt_from_dask(x, 'm')
    >>> x_da
    unyt_dask_array<random_sample, shape=(10000, 10000), dtype=float64, ...
                    chunksize=(1000, 1000), chunktype=numpy.ndarray, units=m>
    >>> x_da.units
    m
    >>> x_da.mean().units()
    m
    >>> x_da.mean().compute()
    unyt_array(0.50001502, 'm')
    >>> x_da.to('cm').mean().compute()
    unyt_array(50.00150242, 'cm')
    >>> (x_da.to('cm')**2).mean().compute()
    unyt_array(3333.37805754, 'cm**2')

    """

    # reduce the dask array to pull out the arguments required for instantiating
    # a new dask.array.core.Array object and then initialize our new unyt_dask
    # array
    (cls, args) = dask_array.__reduce__()

    da = unyt_dask_array(
        *args,
        units=units,
        registry=registry,
        bypass_validation=bypass_validation,
        unyt_name=unyt_name
    )

    return da
