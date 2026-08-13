"""ROS 2 message definitions used by the synthetic generator.

Written out in full so the generated bags are decodable by any ROS 2 tooling, not
just ours — a fixture that only our own reader understands proves nothing.
"""

from __future__ import annotations

SEP = "=" * 80

TIME = """\
int32 sec
uint32 nanosec
"""

HEADER = f"""\
builtin_interfaces/Time stamp
string frame_id
{SEP}
MSG: builtin_interfaces/Time
{TIME}"""

VECTOR3 = """\
float64 x
float64 y
float64 z
"""

QUATERNION = """\
float64 x
float64 y
float64 z
float64 w
"""

FLOAT64 = "float64 data\n"

TWIST = f"""\
geometry_msgs/Vector3 linear
geometry_msgs/Vector3 angular
{SEP}
MSG: geometry_msgs/Vector3
{VECTOR3}"""

IMU = f"""\
std_msgs/Header header
geometry_msgs/Quaternion orientation
float64[9] orientation_covariance
geometry_msgs/Vector3 angular_velocity
float64[9] angular_velocity_covariance
geometry_msgs/Vector3 linear_acceleration
float64[9] linear_acceleration_covariance
{SEP}
MSG: std_msgs/Header
{HEADER}{SEP}
MSG: geometry_msgs/Quaternion
{QUATERNION}{SEP}
MSG: geometry_msgs/Vector3
{VECTOR3}"""

ODOMETRY = f"""\
std_msgs/Header header
string child_frame_id
geometry_msgs/PoseWithCovariance pose
geometry_msgs/TwistWithCovariance twist
{SEP}
MSG: std_msgs/Header
{HEADER}{SEP}
MSG: geometry_msgs/PoseWithCovariance
geometry_msgs/Pose pose
float64[36] covariance
{SEP}
MSG: geometry_msgs/Pose
geometry_msgs/Point position
geometry_msgs/Quaternion orientation
{SEP}
MSG: geometry_msgs/Point
{VECTOR3}{SEP}
MSG: geometry_msgs/Quaternion
{QUATERNION}{SEP}
MSG: geometry_msgs/TwistWithCovariance
geometry_msgs/Twist twist
float64[36] covariance
{SEP}
MSG: geometry_msgs/Twist
geometry_msgs/Vector3 linear
geometry_msgs/Vector3 angular
{SEP}
MSG: geometry_msgs/Vector3
{VECTOR3}"""

LASERSCAN = f"""\
std_msgs/Header header
float32 angle_min
float32 angle_max
float32 angle_increment
float32 time_increment
float32 scan_time
float32 range_min
float32 range_max
float32[] ranges
float32[] intensities
{SEP}
MSG: std_msgs/Header
{HEADER}"""

COMPRESSED_IMAGE = f"""\
std_msgs/Header header
string format
uint8[] data
{SEP}
MSG: std_msgs/Header
{HEADER}"""

LOG = f"""\
builtin_interfaces/Time stamp
uint8 level
string name
string msg
string file
string function
uint32 line
{SEP}
MSG: builtin_interfaces/Time
{TIME}"""

TF_MESSAGE = f"""\
geometry_msgs/TransformStamped[] transforms
{SEP}
MSG: geometry_msgs/TransformStamped
std_msgs/Header header
string child_frame_id
geometry_msgs/Transform transform
{SEP}
MSG: std_msgs/Header
{HEADER}{SEP}
MSG: geometry_msgs/Transform
geometry_msgs/Vector3 translation
geometry_msgs/Quaternion rotation
{SEP}
MSG: geometry_msgs/Vector3
{VECTOR3}{SEP}
MSG: geometry_msgs/Quaternion
{QUATERNION}"""

DIAGNOSTIC_ARRAY = f"""\
std_msgs/Header header
diagnostic_msgs/DiagnosticStatus[] status
{SEP}
MSG: std_msgs/Header
{HEADER}{SEP}
MSG: diagnostic_msgs/DiagnosticStatus
byte level
string name
string message
string hardware_id
diagnostic_msgs/KeyValue[] values
{SEP}
MSG: diagnostic_msgs/KeyValue
string key
string value
"""

MSGDEFS: dict[str, str] = {
    "std_msgs/msg/Float64": FLOAT64,
    "geometry_msgs/msg/Twist": TWIST,
    "sensor_msgs/msg/Imu": IMU,
    "nav_msgs/msg/Odometry": ODOMETRY,
    "sensor_msgs/msg/LaserScan": LASERSCAN,
    "sensor_msgs/msg/CompressedImage": COMPRESSED_IMAGE,
    "rcl_interfaces/msg/Log": LOG,
    "tf2_msgs/msg/TFMessage": TF_MESSAGE,
    "diagnostic_msgs/msg/DiagnosticArray": DIAGNOSTIC_ARRAY,
}
